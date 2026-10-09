"""Exercise native transport through Linux or Wine, never instrument outputs."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import sys

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("binary", type=Path, nargs="?")
parser.add_argument("--wine", action="store_true")
parser.add_argument("--deployed", action="store_true", help="construct deployed workers/DLLs only; never open an instrument")
args = parser.parse_args()
if bool(args.binary) == args.deployed:
    parser.error("choose a fault-test binary or --deployed")
spec = importlib.util.spec_from_file_location("native_smoke_transport", ROOT / "native/python/_native_worker.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if args.wine:
    os.environ.setdefault("WINEPREFIX", "/tmp/esibd-native-wine")
    os.environ.setdefault("WINEDEBUG", "-all")


def command(binary, family):
    return (["wine"] if args.wine else []) + [str(binary.resolve()), "--family", family]


def deployed():
    target = "windows-x86_64" if args.wine or sys.platform == "win32" else "linux-x86_64"
    families = {"ampr": ("ampr_a", "COM-AMPR-12.dll"), "amx": ("amx_a", "COM-HVAMX4ED.dll"),
                "amx_hd": ("amx_hd", "COM-HVAMX4EDH.dll"), "dmmr": ("dmmr", "COM-DMMR-8.dll"),
                "esi": ("esi", "COM-ESI-CTRL.dll"), "psu": ("psu_a", "COM-HVPSU2D.dll"),
                "mscan": ("mscan", None), "tpg366": ("tpg366", None), "transmission": ("transmission", None)}
    reports = []
    for family, (folder, dll_name) in families.items():
        root = ROOT / folder
        manifest = json.loads((root / "native/manifest.json").read_text())
        spec = manifest["targets"].get(target)
        if spec is None:
            continue
        binary = root / "native" / spec["file"]
        assert binary.is_file() and not binary.is_symlink() and binary.parent == root / "native"
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == spec["sha256"]
        config = {}
        if dll_name:
            dll = (root / "vendor/runtime" / family / "vendor/x64" / dll_name).resolve()
            assert dll.is_file()
            dll_path = str(PureWindowsPath("Z:/", *dll.parts[1:])) if args.wine else str(dll)
            config = {"device_id": f"smoke_{family}", "com": 1, "baudrate": 230400, "dll_path": dll_path}
        elif family == "tpg366":
            config = {"port": "COM1" if target.startswith("windows-") else "/dev/no-hardware-smoke"}
        proxy = module.NativeWorkerProxy(root, family, config, command=command(binary, family), startup_timeout_s=90.)
        try:
            assert proxy.identity["family"] == family
            if dll_name or family == "tpg366":
                assert proxy.get_attribute("connected") is False
        finally:
            assert proxy.close(grace_s=0) and proxy._process.poll() is not None
        # No deployment may contain the development fault-injection backend.
        wrong_family = "mscan" if family != "mscan" else "transmission"
        for rejected in ("test", wrong_family):
            try:
                bad = module.NativeWorkerProxy(root, rejected, {}, command=command(binary, rejected), startup_timeout_s=90.)
            except NotImplementedError:
                pass
            else:
                bad.close(grace_s=0)
                raise AssertionError(f"{family} deployment accepts the {rejected} backend")
        reports.append({"family": family, "dll_loaded": bool(dll_name), "connected": False,
                        "test_backend": "rejected", "other_family": "rejected", "reaped": True})
    assert len(reports) == (9 if target.startswith("windows-") else 3)
    print(json.dumps({"platform": "wine" if args.wine else target, "workers": reports,
                      "instrument_open_calls": 0, "hardware": "not tested"}))


if args.deployed:
    deployed()
    raise SystemExit(0)

launch = command(args.binary, "test")
first = module.NativeWorkerProxy(ROOT, "test", {}, command=launch, startup_timeout_s=90., stop_grace_s=.1)
second = None
try:
    assert first.call("echo", ("one", {1: True})) == ("one", {1: True})
    assert first.call("stdout") is True
    assert first.call("echo", "after stdout") == "after stdout"
    second = module.NativeWorkerProxy(ROOT, "test", {}, command=launch, startup_timeout_s=90., stop_grace_s=.1)
    try:
        first.call("hang", rpc_timeout_s=.2)
        raise AssertionError("Blocked worker should have timed out")
    except TimeoutError:
        pass
    assert first.closed and first._process.poll() is not None
    assert second.call("echo", "other plugin alive") == "other plugin alive"
    try:
        second.call("native_hang", rpc_timeout_s=300., _io_timeout_s=.05)
        raise AssertionError("DLL watchdog must not wait for the batch deadline")
    except TimeoutError:
        pass
    assert second.closed and second._process.poll() is not None
    print(json.dumps({"platform": "wine" if args.wine else "linux", "echo": "passed", "stdout_isolation": "passed", "blocked_worker_reaped": "passed", "native_io_watchdog": "passed", "other_worker": "alive until its own fault", "hardware": "not tested"}))
finally:
    first.close(grace_s=0)
    if second is not None:
        second.close(grace_s=0)
