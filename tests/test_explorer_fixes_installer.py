"""The standalone host installer is bounded, idempotent and rollback-safe."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def installer():
    spec = importlib.util.spec_from_file_location("_host_fixes_installer_test", Path(__file__).resolve().parents[1] / "install_explorer_fixes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "esibd"
    root.mkdir()
    (root / "core.py").write_text('''class Channel:
    def restore(self, item, default):
        self.print(f'Added missing parameter {name} to channel {item[self.NAME]} using default value {default[self.VALUE]}.')
class PlotItem(pg.PlotItem):
    def mouseMoveEvent(self, pos):
        label = f'y = {pos.y():.2f}'
class Manager:
    def load(self):
        if exists:
            self.confParser.read(self.pluginFile)
        if exists:
            confParser.read(self.pluginFile)
''')
    (root / "plugins.py").write_text('''def load(self, file):
    confParser.read(file)
    confParser.read(self.activeFileFullPath)
''')
    return root


def test_installer_check_backup_and_idempotence(installer, package, tmp_path):
    original = {path.name: path.read_bytes() for path in package.iterdir()}
    result = installer.install(package, check=True)
    assert result["changed_files"] == 2
    assert {path.name: path.read_bytes() for path in package.iterdir()} == original
    result = installer.install(package, backup_root=tmp_path / "backup")
    assert result["changed_files"] == 2
    assert {path.name: path.read_bytes() for path in Path(result["backup"]).iterdir()} == original
    assert installer.install(package)["changed_files"] == 0


def test_incompatible_second_file_changes_nothing(installer, package, tmp_path):
    (package / "plugins.py").write_text("# incompatible layout\n")
    original = {path.name: path.read_bytes() for path in package.iterdir()}
    with pytest.raises(ValueError, match="Unsupported"):
        installer.install(package, backup_root=tmp_path / "backup")
    assert {path.name: path.read_bytes() for path in package.iterdir()} == original
    assert not (tmp_path / "backup").exists()


def test_retiring_legacy_probe_preserves_original_and_experiment_evidence(installer, tmp_path):
    esi = tmp_path / "esi"
    esi.mkdir()
    old = esi / "esi_hv_activation_probe.ipynb"
    old.write_bytes(b'{"cells": [], "saved_user_output": true}')
    log = esi / "run.json"
    log.write_bytes(b'{"shutdown_confirmed": false}')
    before = old.read_bytes()
    assert installer.retire_legacy_probe(tmp_path, check=True) == str(old)
    assert old.read_bytes() == before
    backup = installer.retire_legacy_probe(tmp_path, backup_root=tmp_path / "archive")
    assert Path(backup).read_bytes() == before and not old.exists()
    assert log.read_bytes() == b'{"shutdown_confirmed": false}'
    assert installer.retire_legacy_probe(tmp_path) is None


def test_failure_writing_second_file_rolls_back_first(installer, package, tmp_path, monkeypatch):
    original = {path.name: path.read_bytes() for path in package.iterdir()}
    atomic = installer.atomic_write
    def fail_second(path, data, mode):
        if path.name == "plugins.py":
            raise OSError("Injected write failure")
        atomic(path, data, mode)
    monkeypatch.setattr(installer, "atomic_write", fail_second)
    with pytest.raises(OSError, match="Injected"):
        installer.install(package, backup_root=tmp_path / "backup")
    assert {path.name: path.read_bytes() for path in package.iterdir()} == original
