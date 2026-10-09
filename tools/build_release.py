"""Build the release ZIP from the git-tracked files of the plugin folders.

    python3 tools/build_release.py 0.3.0      # -> esibd-explorer-plugins-v0.3.0.zip

The archive holds exactly the plugin folders (every folder with a *_plugin.py),
without READMEs, tests, logs, caches or .gitignore. Entries use the last commit
time, so rebuilding the same commit gives the same SHA-256.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_NAMES = {"README.md", ".gitignore"}
EXCLUDED_PARTS = {"tests", "__pycache__", "logs"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def plugin_folders() -> list[str]:
    return sorted({path.parent.name for path in ROOT.glob("*/*_plugin.py")
                   if path.parent.name not in EXCLUDED_PARTS | {"tools", "native"}})


def release_files(folders: list[str]) -> list[str]:
    tracked = _git("ls-files", "-z", "--", *folders).split("\0")
    return sorted(name for name in tracked if name
                  and Path(name).name not in EXCLUDED_NAMES
                  and not EXCLUDED_PARTS.intersection(Path(name).parts)
                  and not name.endswith(".pyc"))


def _validate_native_files(folders: list[str], files: list[str]) -> dict[str, int]:
    spec = importlib.util.spec_from_file_location("_release_native_check", Path(__file__).with_name("build_native_workers.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.ROOT = ROOT
    failures = tool.check(check_sources=False)
    if failures:
        raise RuntimeError("Invalid native bundle:\n" + "\n".join(failures))
    tracked = set(files)
    modes = {}
    for entry in _git("ls-files", "--stage", "-z", "--", *folders).split("\0"):
        if entry:
            metadata, name = entry.split("\t", 1)
            mode, _, stage = metadata.split(" ")
            if stage != "0" or mode not in {"100644", "100755"}:
                raise RuntimeError(f"Non-regular or conflicted tracked plugin file: {name}")
            modes[name] = int(mode, 8)
    for family, slugs in tool.FOLDERS.items():
        for slug in slugs:
            manifest_path = ROOT / slug / "native/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            runtime = "vendor/runtime" if family not in {"mscan", "transmission", "tpg366"} else "_runtime"
            required = {f"{slug}/native/manifest.json", f"{slug}/{runtime}/_native_worker.py"}
            records = [*manifest["targets"].values(), manifest["source"], manifest["third_party_notices"]]
            required.update(f"{slug}/native/{record['file']}" for record in records)
            missing = required - tracked
            if missing:
                raise RuntimeError("Native assets must be tracked before packaging: " + ", ".join(sorted(missing)))
            for target, record in manifest["targets"].items():
                name = f"{slug}/native/{record['file']}"
                expected = 0o100755 if target.startswith("linux-") else 0o100644
                if modes[name] != expected:
                    raise RuntimeError(f"Incorrect tracked worker permissions: {name}")
    for name in files:
        path = ROOT / name
        if "vendor/python" in path.relative_to(ROOT).as_posix():
            raise RuntimeError(f"Private Python is not part of the native bundle: {name}")
        for candidate in (path, *path.parents):
            if candidate.is_symlink():
                raise RuntimeError(f"Symlink not permitted in release: {candidate}")
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise RuntimeError(f"Missing or non-regular release file: {name}")
    return modes


def build(version: str, output_dir: Path = ROOT, *, allow_dirty: bool = False) -> Path:
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        raise ValueError(f"Version must look like 0.3.0, got {version!r}")
    folders = plugin_folders()
    if not allow_dirty and _git("status", "--porcelain", "--", *folders).strip():
        raise RuntimeError("Plugin folders have uncommitted changes; commit them first.")
    files = release_files(folders)
    modes = _validate_native_files(folders, files)
    stamp = datetime.fromtimestamp(int(_git("log", "-1", "--format=%ct").strip()), timezone.utc)
    date_time = (max(stamp.year, 1980), stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)
    archive = Path(output_dir) / f"esibd-explorer-plugins-v{version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for name in files:
            info = zipfile.ZipInfo(name, date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = modes[name] << 16
            output.writestr(info, (ROOT / name).read_bytes())
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="release version without the leading v, e.g. 0.3.0")
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument("--allow-dirty", action="store_true", help="build from uncommitted plugin files (testing only)")
    args = parser.parse_args(argv)
    archive = build(args.version, args.output_dir, allow_dirty=args.allow_dirty)
    with zipfile.ZipFile(archive) as built:
        count = len(built.namelist())
    print(f"{archive.name}: {count} files, SHA-256 {hashlib.sha256(archive.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
