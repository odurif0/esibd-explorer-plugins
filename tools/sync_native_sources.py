"""Replicate native supervision sources without sharing deployed runtime state."""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import stat

ROOT = Path(__file__).resolve().parents[1]
DEVICES = ("ampr_a", "amx_a", "amx_hd", "dmmr", "esi", "psu_a")
ALL_DEVICES = ("ampr_a", "ampr_b", "amx_a", "amx_b", "amx_hd", "dmmr", "esi", "psu_a", "psu_b", "psu_c", "psu_d", "psu_e")


def _regular_path(path):
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"Symlink not permitted: {candidate}")
    if path.exists() and not (path.is_dir() or stat.S_ISREG(path.stat().st_mode)):
        raise ValueError(f"Non-regular plugin asset: {path}")


def sync(check=False):
    sources = [(ROOT / "native/python/_native_worker.py", ROOT / folder / "vendor/runtime/_native_worker.py") for folder in ALL_DEVICES]
    sources += [(ROOT / "native/python/_native_worker.py", ROOT / folder / "_runtime/_native_worker.py") for folder in ("tpg366", "transmission")]
    sources += [(ROOT / "native/python/_native_worker.py", ROOT / "mscan/_runtime/_native_worker.py")]
    canonical = ROOT / "amx_a/vendor/runtime/_driver_common.py"
    sources += [(canonical, ROOT / folder / "vendor/runtime/_driver_common.py") for folder in ALL_DEVICES if folder != "amx_a"]
    drift = []
    for source, target in sources:
        _regular_path(source)
        _regular_path(target)
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            drift.append(str(target.relative_to(ROOT)))
            if not check:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    return drift


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    paths = sync(args.check)
    for path in paths:
        print(path)
    raise SystemExit(bool(args.check and paths))
