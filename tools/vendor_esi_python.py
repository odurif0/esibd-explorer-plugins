"""Extract the pinned official Windows Python used only by the ESI worker.

Download ARCHIVE_URL, then run: python3 tools/vendor_esi_python.py /path/to/archive.zip
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    metadata = vendor(args.archive)
    print(f"ESI private Python {VERSION}: {len(metadata['files'])} files, "
          f"{metadata['uncompressed_bytes']} bytes extracted")
