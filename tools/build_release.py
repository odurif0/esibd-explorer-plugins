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
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_NAMES = {"README.md", ".gitignore"}
EXCLUDED_PARTS = {"tests", "__pycache__", "logs"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def plugin_folders() -> list[str]:
    return sorted({path.parent.name for path in ROOT.glob("*/*_plugin.py")})


def release_files(folders: list[str]) -> list[str]:
    tracked = _git("ls-files", "-z", "--", *folders).split("\0")
    return sorted(name for name in tracked if name
                  and Path(name).name not in EXCLUDED_NAMES
                  and not EXCLUDED_PARTS.intersection(Path(name).parts)
                  and not name.endswith(".pyc"))


def build(version: str, output_dir: Path = ROOT, *, allow_dirty: bool = False) -> Path:
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        raise ValueError(f"Version must look like 0.3.0, got {version!r}")
    folders = plugin_folders()
    if not allow_dirty and _git("status", "--porcelain", "--", *folders).strip():
        raise RuntimeError("Plugin folders have uncommitted changes; commit them first.")
    stamp = datetime.fromtimestamp(int(_git("log", "-1", "--format=%ct").strip()), timezone.utc)
    date_time = (max(stamp.year, 1980), stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)
    archive = Path(output_dir) / f"esibd-explorer-plugins-v{version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for name in release_files(folders):
            info = zipfile.ZipInfo(name, date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
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
