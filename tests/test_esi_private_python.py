"""Private Windows Python integrity and opt-in hardware-free worker execution."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import struct
import sys
import threading

import pytest

from conftest import PLUGIN_SPECS
from test_esi_process_recovery import FAKE_CONTROLLER, WORKER


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "esi/vendor/python"
MANIFEST = json.loads((PYTHON / "manifest.json").read_text())


def test_private_python_matches_official_source_and_release_requirements():
    spec = importlib.util.spec_from_file_location("esi_python_vendor_tool", ROOT / "tools/vendor_esi_python.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert MANIFEST["version"] == tool.VERSION
    assert MANIFEST["source_url"] == tool.ARCHIVE_URL
    assert MANIFEST["archive_sha256"] == tool.ARCHIVE_SHA256
    expected = {*MANIFEST["files"], "manifest.json"}
    assert {path.name for path in PYTHON.iterdir()} == expected
    esi = next(spec for spec in PLUGIN_SPECS if spec.folder == "esi")
    assert {f"vendor/python/{name}" for name in expected} <= set(esi.bundled_files)
    assert MANIFEST["uncompressed_bytes"] == sum(file["bytes"] for file in MANIFEST["files"].values())


@pytest.mark.parametrize("name,metadata", MANIFEST["files"].items(), ids=MANIFEST["files"])
def test_private_python_files_are_regular_and_unchanged(name, metadata):
    path = PYTHON / name
    assert path.is_file() and not path.is_symlink()
    data = path.read_bytes()
    assert len(data) == metadata["bytes"]
    assert hashlib.sha256(data).hexdigest() == metadata["sha256"]
    if path.suffix in (".exe", ".dll", ".pyd"):
        assert data[:2] == b"MZ"
        offset, = struct.unpack_from("<I", data, 0x3C)
        assert data[offset:offset + 4] == b"PE\0\0"
        machine, = struct.unpack_from("<H", data, offset + 4)
        assert machine == 0x8664, "private Python must use AMD64 binaries"


def test_vendoring_refuses_an_unverified_archive_without_writing_files(tmp_path):
    spec = importlib.util.spec_from_file_location("esi_python_vendor_tool", ROOT / "tools/vendor_esi_python.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    archive = tmp_path / "modified.zip"
    archive.write_bytes(b"not the official archive")
    destination = tmp_path / "vendor"
    with pytest.raises(ValueError, match="official Python SHA-256"):
        tool.vendor(archive, destination)
    assert not destination.exists()


@pytest.mark.slow
@pytest.mark.skipif(not sys.platform.startswith("win") and not os.environ.get("ESIBD_ESI_TEST_WINE"),
                    reason="requires Windows or explicit ESIBD_ESI_TEST_WINE=1")
@pytest.mark.parametrize("operation", ["roundtrip", "crash", "block", "vendor_bootstrap"])
def test_private_windows_python_worker_without_hardware(tmp_path, monkeypatch, operation):
    spec = importlib.util.spec_from_file_location("esi_private_python_probe", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("ESIBD_ESI_WORKER_PYTHON", "invalid external Python must be ignored")

    def windows_path(path):
        path = Path(path).resolve()
        return str(path) if sys.platform.startswith("win") else str(PureWindowsPath("Z:/", *path.parts[1:]))

    if not sys.platform.startswith("win"):
        wine = shutil.which("wine")
        if not wine:
            pytest.skip("Wine is not installed")
        original_popen = module.subprocess.Popen

        def launch(command, **kwargs):
            return original_popen([wine, windows_path(command[0]), *command[1:-1], windows_path(command[-1])], **kwargs)

        monkeypatch.setattr(module.subprocess, "Popen", launch)

    fake = tmp_path / "controller.py"
    fake.write_text(FAKE_CONTROLLER)
    # The real vendor case only constructs the controller; it never opens a port.
    log_dir = tmp_path / "data/logs/esi"
    kwargs = {"device_id": "private_python_bootstrap", "com": 16,
              "log_dir": windows_path(log_dir)} if operation == "vendor_bootstrap" else {}
    proxy = module.ESIProcessProxy(kwargs, controller_file=None if kwargs else windows_path(fake), startup_timeout_s=30.)
    try:
        assert proxy._command[0] == str(PYTHON / "python.exe")
        if operation == "vendor_bootstrap":
            assert proxy.get_attribute("connected", timeout_s=3.) is False
            assert (log_dir / "esi_private_python_bootstrap.log").is_file()
        elif operation == "roundtrip":
            assert proxy.call_method("ping", rpc_timeout_s=3.) == (b"config bytes", {1: (True, 700.)})
            cancel = threading.Event()
            reports = []

            def progress(report):
                reports.append(report)
                cancel.set()

            assert proxy.call_method("block", rpc_timeout_s=3., cancel_event=cancel, on_discharge=progress)
            assert reports == [{"entered": True}]
        else:
            with pytest.raises(RuntimeError):
                proxy.call_method(operation, rpc_timeout_s=.3)
            assert proxy.closed and proxy._process.poll() is not None
    finally:
        assert proxy.close()
