"""CGC connection-recovery rigs shared by the CGC plugin tests."""
from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import weakref

import pytest

from support.shutdown import FAMILIES, controller_for

ROOT = Path(__file__).resolve().parents[2]
SPECS = [
    ("ampr_a", "ampr", "AMPR"), ("ampr_b", "ampr", "AMPR"),
    ("amx_a", "amx", "AMX"), ("amx_b", "amx", "AMX"),
    ("amx_hd", "amx_hd", "AMXHD"),
    *((f"psu_{letter}", "psu", "PSU") for letter in "abcde"),
    ("esi", "esi", "ESI"),
]


class NativePort:
    def __init__(self):
        self.operations = []
        self.workers = set()
        self.open_entered = threading.Event()
        self.open_release = threading.Event()
        self.open_release.set()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.close_release.set()
        self.baud_entered = threading.Event()
        self.baud_release = threading.Event()
        self.baud_release.set()
        self.open_status = 0
        self.close_status = 0
        self.allocate_handle = True
        self.handle_owner = None
        self.active = 0
        self.guard = threading.Lock()

    def invoke(self, name, device, event=None, release=None, result=0):
        with self.guard:
            assert self.active == 0, "native calls must never overlap"
            self.active += 1
            self.workers.add(threading.current_thread())
            self.operations.append((name, id(device)))
        try:
            if event is not None:
                event.set()
            if release is not None:
                assert release.wait(5), f"test did not release {name}"
            return result
        finally:
            with self.guard:
                self.active -= 1

    def open(self, device, *args):
        assert self.handle_owner is None, "Open must not reuse an owned native channel"
        if self.allocate_handle:
            self.handle_owner = id(device)
        return self.invoke("open", device, self.open_entered,
                           self.open_release, self.open_status)

    def close(self, device, *args):
        status = self.invoke("close", device, self.close_entered,
                             self.close_release, self.close_status)
        if status == 0:
            self.handle_owner = None
        return status

    def baud(self, device, baud):
        return self.invoke("baud", device, self.baud_entered,
                           self.baud_release, (0, baud))

    def count(self, name, device=None):
        return sum(op == name and (device is None or owner == id(device))
                   for op, owner in self.operations)

    def finish_open(self):
        self.open_release.set()
        self.join_workers()

    def join_workers(self):
        for worker in tuple(self.workers):
            if worker is not threading.current_thread():
                worker.join(2)
                assert not worker.is_alive()


@pytest.fixture(params=SPECS, ids=[spec[0] for spec in SPECS])
def cgc_rig(request, monkeypatch):
    slug, family, name = request.param
    namespace = f"_connection_recovery_{slug}"
    runtime = ROOT / slug / "vendor" / "runtime"
    spec = importlib.util.spec_from_file_location(
        namespace, runtime / "__init__.py", submodule_search_locations=[str(runtime)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = package
    spec.loader.exec_module(package)
    module = importlib.import_module(f"{namespace}.{family}.{family}")
    cls = getattr(module, f"_{name}Controller")
    base = getattr(module, f"{name}Base")
    native = NativePort()
    logger = logging.getLogger(f"test.initial_connection.{slug}")
    monkeypatch.setattr(base, "__init__", lambda self, *a, **kw: setattr(self, "err_dict", {}))
    # Use descriptors, not a bound NativePort method: production passes either
    # a bound super() method or an unbound base method with the device argument.
    monkeypatch.setattr(base, "open_port", lambda self, *a: native.open(self, *a))
    monkeypatch.setattr(base, "close_port", lambda self, *a: native.close(self, *a))
    for method in ("set_baud_rate", "set_comspeed"):
        if hasattr(base, method):
            monkeypatch.setattr(base, method, lambda self, baud: native.baud(self, baud))
    for method in ("get_device_type", "get_dev_type"):
        if hasattr(base, method):
            monkeypatch.setattr(base, method, lambda self: (0, self.DEVICE_TYPE))
    if hasattr(cls, "_warn_if_unexpected_product_id"):
        monkeypatch.setattr(cls, "_warn_if_unexpected_product_id", lambda *a, **kw: None)
    if family == "esi":
        monkeypatch.setattr(cls, "_prepare_safe_inventory", lambda self, timeout: None)
        monkeypatch.setattr(cls, "force_safe_off", lambda self, **kw: True)
        monkeypatch.setattr(cls, "discover_modules", lambda self, **kw: {})
        monkeypatch.setattr(cls, "set_global_active", lambda self, *a, **kw: None)
        monkeypatch.setattr(cls, "_verify_hv_discharge", lambda self, *a, **kw: None)
    instances = weakref.WeakSet()

    def new():
        device = cls(f"retry_{slug}_{len(instances)}", com=15, logger=logger)
        device._DEFAULT_IO_TIMEOUT_S = 0.05
        instances.add(device)
        return device

    def disconnect(device):
        if family == "ampr":
            return device.disconnect()
        return device.disconnect(timeout_s=0.05)

    yield SimpleNamespace(new=new, disconnect=disconnect, hw=native, cls=cls,
                          base=base, family=family)
    native.open_release.set()
    native.close_release.set()
    native.baud_release.set()
    native.join_workers()
    # Each case imports its own runtime. Retire only its simulated registry;
    # no native library, real port or other test's owner is touched.
    if hasattr(cls, "_active_connections"):
        cls._active_connections.clear()
    if hasattr(cls, "_connected_instance"):
        cls._connected_instance = None
    for key in list(sys.modules):
        if key == namespace or key.startswith(namespace + "."):
            sys.modules.pop(key, None)


@pytest.fixture
def cgc_plugin(cgc_rig, monkeypatch, tmp_path):
    return build_plugin(cgc_rig, monkeypatch, data_path=tmp_path)


def build_plugin(rig, monkeypatch, *, data_path=None):
    module, controller, messages = controller_for(rig.family)
    parent = controller.controllerParent
    # The Qt subprocess probes also call this test-only builder without fixtures.
    parent.pluginManager = SimpleNamespace(Settings=SimpleNamespace(
        dataPath=Path(tempfile.gettempdir()) if data_path is None else Path(data_path)))
    parent.com, parent.baudrate = 15, 9600
    parent.connect_timeout_s = parent.poll_timeout_s = .05
    parent.heat_voltage_limit_v = parent.heat_current_limit_a = parent.heat_power_limit_w = 0
    parent.onAction = SimpleNamespace(state=True)
    parent.isOn = lambda: parent.onAction.state
    parent._set_on_ui_state = lambda value: setattr(parent.onAction, 'state', bool(value))
    controller.initialized = False
    controller.initializing = False

    def launch(self):
        if self.initializing:
            return
        self.initializing = True
        self.runInitialization()

    # Keep the real controller's explicit-request hook; only replace Explorer's
    # thread launcher. A worker call alone is not a fresh user ON request.
    monkeypatch.setattr(module.DeviceController, 'initializeCommunication', launch, raising=False)
    for name in ('_refresh_module_scan', '_refresh_available_configs',
                 '_refresh_loaded_config_status', '_update_state', '_sync_status_to_gui',
                 '_sync_status', '_apply_interlock_setting_unlocked', '_apply_snapshot'):
        if hasattr(controller, name):
            monkeypatch.setattr(controller, name, lambda *a, **kw: None)
    created, successes = [], []

    def factory(**kwargs):
        device = rig.new()
        device.close = lambda: rig.hw.invoke('dispose', device)
        if rig.family == 'esi':
            device.collect_identity = lambda **kw: {}
            device.collect_diagnostics = lambda **kw: {}
            device.configure_hv_max_voltage_steps = lambda *a, **kw: None
        created.append(device)
        return device

    # Diagnostics must not interrogate a failed opening, even one that returned
    # an error without poisoning the worker (and may own a partial handle).
    for name in ('get_state', 'get_device_state', 'get_voltage_state',
                 'get_interlock_state', 'get_controller_state'):
        if hasattr(rig.base, name):
            monkeypatch.setattr(rig.base, name, lambda self, *a, **kw:
                                rig.hw.invoke('diagnostic_read', self, result=(0, 0, 'ST_OFF')))
    # Inject the inline Python reference explicitly for these DLL fault tests;
    # production initialization still selects the native worker with no fallback.
    monkeypatch.setattr(module, FAMILIES[rig.family][2], lambda: factory)
    controller.signalComm = SimpleNamespace(
        initCompleteSignal=SimpleNamespace(emit=lambda: successes.append(True)))
    return SimpleNamespace(module=module, controller=controller, parent=parent, created=created,
                           successes=successes, messages=messages)
