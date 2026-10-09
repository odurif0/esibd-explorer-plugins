"""Build per-family workers locally; deploy independent plugin-local binaries."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = {"ampr": ("ampr_a", "ampr_b"), "amx": ("amx_a", "amx_b"),
           "amx_hd": ("amx_hd",), "dmmr": ("dmmr",), "esi": ("esi",),
           "psu": ("psu_a", "psu_b", "psu_c", "psu_d", "psu_e"),
           "tpg366": ("tpg366",), "mscan": ("mscan",), "transmission": ("transmission",)}
TARGETS = {"x86_64-pc-windows-gnu": ("windows-x86_64", ".exe"),
           "x86_64-pc-windows-msvc": ("windows-x86_64", ".exe"),
           "x86_64-unknown-linux-gnu": ("linux-x86_64", "")}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _regular_tree(path):
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"Symlink not permitted: {candidate}")
    if path.exists() and not (path.is_dir() or stat.S_ISREG(path.stat().st_mode)):
        raise ValueError(f"Non-regular native asset: {path}")


def _sources():
    path = Path(__file__).with_name("bundle_native_sources.py")
    spec = importlib.util.spec_from_file_location("_esibd_native_source_builder", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.ROOT = ROOT
    return module


def build(families, target, *, deploy=False, zig=None, output=None):
    families = tuple(families)
    if not families or any(family not in FOLDERS for family in families):
        raise ValueError("Choose at least one supported native family")
    key, extension = TARGETS[target]
    source_builder = _sources()
    source_hash = source_builder.source_data_sha256()
    compiler = subprocess.run(["rustc", "--version"], check=True, capture_output=True, text=True).stdout.strip()
    environment = os.environ.copy()
    if zig:
        if target != "x86_64-pc-windows-gnu":
            raise ValueError("Zig cross-linking requires the Windows GNU target")
        environment.update(ESIBD_ZIG=str(Path(zig).resolve()),
                           ZIG_GLOBAL_CACHE_DIR=str(Path(os.environ.get("TMPDIR", "/tmp")) / "esibd-zig-cache"),
                           CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=str(ROOT / "native/zig-linker.py"),
                           CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUSTFLAGS=f"-C dlltool={ROOT / 'native/zig-dlltool.py'} -C panic=abort")
    output = Path(output or ROOT / "native/target/workers") / key
    _regular_tree(output)
    output.mkdir(parents=True, exist_ok=True)
    products = []
    for family in families:
        command = ["cargo", "build", "--locked", "--offline", "--release", "--manifest-path", str(ROOT / "native/Cargo.toml"),
                   "--target", target, "--no-default-features", "--features", family]
        subprocess.run(command, cwd=ROOT, env=environment, check=True)
        if source_builder.source_data_sha256() != source_hash:
            raise RuntimeError("Native sources changed during the build; rebuild from a stable tree")
        source = ROOT / "native/target" / target / "release" / f"esibd-native-worker{extension}"
        product = output / f"esibd-{family}-worker{extension}"
        _regular_tree(product)
        shutil.copy2(source, product)
        product.chmod(0o755 if key.startswith("linux-") else 0o644)
        spec = {"file": product.name, "sha256": digest(product), "size_bytes": product.stat().st_size,
                "target": target, "features": [family], "cargo_lock_sha256": digest(ROOT / "native/Cargo.lock"),
                "source_data_sha256": source_hash, "compiler": compiler}
        products.append({"family": family, **spec})
        if deploy:
            # Canonicals are deployed here; siblings are updated by the normal sync.
            directory = ROOT / FOLDERS[family][0] / "native"
            _regular_tree(directory)
            directory.mkdir(parents=True, exist_ok=True)
            manifest_path = directory / "manifest.json"
            if manifest_path.is_symlink():
                raise ValueError(f"Symlink manifest: {manifest_path}")
            manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {
                "family": family, "protocol": 1, "worker_version": "0.1.0", "targets": {},
                "validation": "hardware-not-validated"}
            if manifest["family"] != family or manifest["protocol"] != 1:
                raise ValueError(f"Incompatible existing manifest: {manifest_path}")
            target_path = directory / product.name
            if target_path.is_symlink():
                raise ValueError(f"Symlink worker: {target_path}")
            shutil.copy2(product, target_path)
            manifest["targets"][key] = spec
            metadata = source_builder.bundle_family(family, directory)
            if metadata["source_data_sha256"] != source_hash:
                raise RuntimeError("Native sources changed during source bundling; rebuild from a stable tree")
            manifest.update(metadata)
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if deploy:
        subprocess.run(["python3", str(ROOT / "tools/sync_native_sources.py")], check=True)
        subprocess.run(["python3", str(ROOT / "tools/sync_family_siblings.py")], check=True)
    return products


def check(*, check_sources=True):
    failures = []
    live_hash = _sources().source_data_sha256() if check_sources else None
    for family, folders in FOLDERS.items():
        for folder in folders:
            directory = ROOT / folder / "native"
            manifest_path = directory / "manifest.json"
            try:
                _regular_tree(directory)
                _regular_tree(manifest_path)
            except ValueError as exc:
                failures.append(f"{folder}: {exc}")
                continue
            if not manifest_path.is_file() or manifest_path.is_symlink():
                failures.append(f"{folder}: missing regular native manifest")
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, ValueError) as exc:
                failures.append(f"{folder}: unreadable native manifest: {exc}")
                continue
            if (not isinstance(manifest, dict) or manifest.get("family") != family
                    or manifest.get("protocol") != 1 or manifest.get("worker_version") != "0.1.0"
                    or not isinstance(manifest.get("targets"), dict) or not manifest["targets"]):
                failures.append(f"{folder}: invalid native manifest")
                continue
            expected_targets = {"windows-x86_64"}
            if family in {"tpg366", "mscan", "transmission"}:
                expected_targets.add("linux-x86_64")
            if set(manifest["targets"]) != expected_targets:
                failures.append(f"{folder}: missing or unexpected native targets")
            source_hash = manifest.get("source_data_sha256")
            lock_hash = manifest.get("cargo_lock_sha256")
            if (not isinstance(source_hash, str) or len(source_hash) != 64
                    or not isinstance(lock_hash, str) or len(lock_hash) != 64
                    or (live_hash is not None and source_hash != live_hash)):
                failures.append(f"{folder}: stale native source provenance")
            records = list(manifest["targets"].values())
            for kind, filename in (("source", "source.zip"), ("third_party_notices", "THIRD_PARTY_NOTICES.txt")):
                metadata = manifest.get(kind)
                if not isinstance(metadata, dict) or metadata.get("file") != filename:
                    failures.append(f"{folder}: missing native {kind} metadata")
                    continue
                if kind == "source" and (metadata.get("source_data_sha256") != source_hash
                        or metadata.get("vendored") is not (family in {"mscan", "tpg366"})):
                    failures.append(f"{folder}: invalid native source metadata")
                records.append(metadata)
            for key, spec in manifest["targets"].items():
                if not isinstance(spec, dict):
                    failures.append(f"{folder}: invalid {key} binary record")
                    continue
                target = spec.get("target")
                if (spec.get("features") != [family] or spec.get("source_data_sha256") != source_hash
                        or spec.get("cargo_lock_sha256") != lock_hash
                        or not isinstance(target, str) or TARGETS.get(target, (None,))[0] != key):
                    failures.append(f"{folder}: stale or incompatible {key} binary provenance")
                if key.startswith("linux-"):
                    filename = spec.get("file", "")
                    if not isinstance(filename, str):
                        failures.append(f"{folder}: invalid {key} binary filename")
                        continue
                    path = directory / filename
                    if path.is_file() and not path.stat().st_mode & 0o111:
                        failures.append(f"{folder}: Linux worker is not executable")
            for spec in records:
                if not isinstance(spec, dict):
                    failures.append(f"{folder}: invalid native asset record")
                    continue
                filename = spec.get("file", "")
                try:
                    if not isinstance(filename, str):
                        raise ValueError("Native asset filename must be a string")
                    path = directory / filename
                    _regular_tree(path)
                    valid = (isinstance(filename, str) and filename and Path(filename).name == filename
                             and path.is_file() and path.stat().st_size == spec.get("size_bytes")
                             and digest(path) == spec.get("sha256"))
                except (OSError, ValueError, TypeError):
                    valid = False
                if not valid:
                    failures.append(f"{folder}: native asset integrity failure: {filename}")
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, default="x86_64-pc-windows-gnu")
    parser.add_argument("--families", nargs="+", choices=FOLDERS, default=list(FOLDERS))
    parser.add_argument("--deploy", action="store_true")
    parser.add_argument("--zig", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        problems = check()
        for problem in problems:
            print(problem)
        raise SystemExit(bool(problems))
    print(json.dumps(build(args.families, args.target, deploy=args.deploy, zig=args.zig, output=args.output), indent=2))
