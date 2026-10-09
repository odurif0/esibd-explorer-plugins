"""Legacy Python-reference tooling; production plugins use native Rust workers.

Use --retire to remove an unchanged obsolete private interpreter bundle.
Explicit --vendor is only for reproducing historical reference-worker tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import zipfile


VERSION = "3.14.8"
ARCHIVE_URL = f"https://www.python.org/ftp/python/{VERSION}/python-{VERSION}-embed-amd64.zip"
ARCHIVE_SHA256 = "a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310"
DESTINATION = Path(__file__).resolve().parents[1] / "esi/vendor/python"


def vendor(archive_path: Path, destination: Path = DESTINATION) -> dict:
    archive_bytes = archive_path.read_bytes()
    if hashlib.sha256(archive_bytes).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Archive does not match the pinned official Python SHA-256")
    with zipfile.ZipFile(archive_path) as archive:
        files = {}
        for entry in archive.infolist():
            if (entry.filename != Path(entry.filename).name or "\\" in entry.filename
                    or entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError(f"Unexpected archive entry: {entry.filename!r}")
            files[entry.filename] = archive.read(entry)
    required = {"python.exe", "python314.dll", "python314.zip", "python314._pth", "LICENSE.txt"}
    if not required <= files.keys():
        raise ValueError("Official archive is missing required Python files")
    if destination.is_symlink():
        raise ValueError("Private Python destination must not be a symlink")
    if destination.exists():
        for path in destination.iterdir():
            if path.is_symlink() or not path.is_file() or path.name not in {*files, "manifest.json"}:
                raise ValueError(f"Refusing to replace unexpected file: {path}")
    manifest = {
        "version": VERSION,
        "architecture": "amd64",
        "source_url": ARCHIVE_URL,
        "archive_sha256": ARCHIVE_SHA256,
        "archive_bytes": len(archive_bytes),
        "uncompressed_bytes": sum(map(len, files.values())),
        "files": {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in sorted(files.items())},
    }
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (destination / name).write_bytes(data)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="ascii")
    return manifest


def retire(destination: Path = DESTINATION) -> dict:
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError(f"Refusing to retire a symlinked Python bundle: {path}")
    manifest_path = destination / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Private Python manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    if (manifest.get("version") != VERSION or manifest.get("archive_sha256") != ARCHIVE_SHA256
            or manifest.get("source_url") != ARCHIVE_URL or not isinstance(manifest.get("files"), dict)):
        raise ValueError("Refusing to remove a different private Python bundle")
    expected = {*manifest["files"], "manifest.json"}
    if {path.name for path in destination.iterdir()} != expected:
        raise ValueError("Refusing to remove unexpected files from the private Python folder")
    for name, metadata in manifest["files"].items():
        if not name or Path(name).name != name or "\\" in name:
            raise ValueError("Invalid private Python manifest filename")
        path = destination / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Refusing to remove a non-regular file: {path}")
        data = path.read_bytes()
        if len(data) != metadata["bytes"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ValueError(f"Refusing to remove a changed Python bundle file: {path}")
    for name in sorted(manifest["files"]):
        (destination / name).unlink()
    manifest_path.unlink()
    destination.rmdir()
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--vendor", type=Path, metavar="ARCHIVE", help="reproduce a historical Python reference bundle")
    operation.add_argument("--retire", action="store_true", help="remove the unchanged obsolete private Python bundle")
    args = parser.parse_args()
    metadata = retire() if args.retire else vendor(args.vendor)
    print(f"ESI legacy private Python {VERSION}: {len(metadata['files'])} files, "
          f"{metadata['uncompressed_bytes']} bytes {'retired' if args.retire else 'extracted'}")
