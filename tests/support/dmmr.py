"""DMMR plugin loading, fake hardware and fault rigs shared by the DMMR tests."""
from __future__ import annotations

import ast
from collections import Counter
import contextlib
import importlib
from importlib.metadata import distribution
import json
import logging
import os
from pathlib import Path
import sys
import threading
import types
from types import MethodType, ModuleType, SimpleNamespace

import numpy as np
import pytest

from explorer_host import compile_definition
from support import explorer


def load_plugin(path: str = "dmmr/dmmr_plugin.py", module_name: str = "dmmr_plugin_behavior_test"):
    """Import a DMMR entry point (the bundled one by default) against a fresh fake Explorer."""
    return explorer.load_plugin(path, module_name, runtimes=("dmmr",))


class ExplorerStyleLock:
    """Explorer 1.0.1 exposes a raw lock AND a log-level-dependent context.

    Its BASIC/DEBUG context swallows exceptions. Its VERBOSE/TRACE context
    propagates them but does not release the lock on that path. Expired waits
    are immediate here so regressions do not spend a second per nested lock.
    """

    def __init__(self, parent, *, swallow=True):
        self._lock = threading.Lock()
        self.parent = parent
        self.swallow = swallow
        self.attempts = 0
        self.context_entries = 0

    def acquire(self, blocking=True, timeout=-1):
        self.attempts += 1
        return self._lock.acquire(blocking=False)

    def release(self):
        self._lock.release()

    @contextlib.contextmanager
    def acquire_timeout(self, timeout, timeoutMessage="", already_acquired=False):
        self.context_entries += 1
        acquired = already_acquired or self.acquire(timeout=timeout)
        if not self.swallow:
            yield acquired
            if acquired and not already_acquired:
                self.release()
            return
        try:
            yield acquired
        except Exception:
            self.parent.errorCount += 1
        finally:
            if acquired and not already_acquired:
                self.release()


class Device:
    NO_ERR = 0
    ERR_COMMAND_WRONG = -13

    def __init__(self):
        self.calls = []
        self.enabled = False
        self.automatic = False
        self.auto_range_failure = None
        self.auto_modes = {}
        self.wrong_command_once = False

    def set_enable(self, enabled, **kwargs):
        self.calls.append(("enable", enabled))
        self.enabled = enabled
        return self.NO_ERR

    def set_automatic_current(self, enabled, **kwargs):
        self.calls.append(("automatic", enabled))
        self.automatic = enabled
        return self.NO_ERR

    def get_enable(self, **kwargs):
        return self.NO_ERR, self.enabled

    def get_automatic_current(self, **kwargs):
        return self.NO_ERR, self.automatic

    def set_module_auto_range(self, address, enabled, **kwargs):
        self.calls.append(("auto_range", address, enabled))
        if address == self.auto_range_failure:
            return -12
        self.auto_modes[address] = enabled
        return self.NO_ERR

    def get_module_meas_range(self, address, **kwargs):
        self.calls.append(("range_readback", address))
        return self.NO_ERR, 0, self.auto_modes.get(address, False)

    def get_state(self, **kwargs):
        self.calls.append(("state",))
        return self.NO_ERR, "0x0000", "ST_ON"

    def get_module_current(self, address, **kwargs):
        self.calls.append(("current", address))
        if self.wrong_command_once:
            self.wrong_command_once = False
            return self.ERR_COMMAND_WRONG, np.nan, 0
        return self.NO_ERR, (address + 1) * 1e-12, 0

    def format_status(self, status):
        return str(status)


@pytest.fixture
def dmmr_acquisition_rig():
    module = load_plugin()
    channels = [types.SimpleNamespace(real=True, module_address=lambda address=address: address)
                for address in (0, 3)]
    ui_states = []
    parent = types.SimpleNamespace(
        connect_timeout_s=5.0, poll_timeout_s=2.0, interval=0,
        isOn=lambda: True, getChannels=lambda: channels,
        getConfiguredModules=lambda: [0, 3],
        _set_on_ui_state=ui_states.append,
    )
    controller = module.DMMRController(parent)
    controller.initialized = True
    controller.detected_module_ids = [0, 3]
    controller.lock = ExplorerStyleLock(controller)
    controller.device = Device()
    logs = []
    controller.print = lambda message, flag=None: logs.append(message)
    return types.SimpleNamespace(module=module, controller=controller,
                                 device=controller.device, logs=logs, ui_states=ui_states)


PollingDevice = Device


class RangeDevice(PollingDevice):
    def __init__(self):
        super().__init__()
        self.modes = {0: True, 3: True}
        self.ranges = {0: 2, 3: 4}
        self.failure = None
        self.samples = {0: (0, 0.534e-12, 0), 3: (0, -2e-12, 4)}

    def set_module_auto_range(self, address, enabled, **kwargs):
        status = super().set_module_auto_range(address, enabled, **kwargs)
        self.modes[address] = enabled
        return -12 if self.failure == "auto_ack" else status

    def set_module_meas_range(self, address, value, **kwargs):
        self.calls.append(("fixed_range", address, value))
        self.ranges[address] = value
        return -12 if self.failure == "range_ack" else 0

    def get_module_meas_range(self, address, **kwargs):
        self.calls.append(("range_readback", address))
        if self.failure == "read_exception":
            raise RuntimeError("range read failed")
        return (
            -12 if self.failure == "read_status" else 0,
            5 if self.failure == "invalid_range" else 2 if self.failure == "wrong_range" else self.ranges[address],
            not self.modes[address] if self.failure == "wrong_auto" else self.modes[address],
        )

    def get_module_current(self, address, **kwargs):
        self.calls.append(("current", address))
        sample = self.samples[address]
        if isinstance(sample, Exception):
            raise sample
        return sample


@pytest.fixture
def dmmr_range_rig():
    module = load_plugin()
    channels = [SimpleNamespace(real=True, enabled=True, monitor=np.nan,
                                _requested_range_mode=mode, module_address=lambda a=a: a)
                for a, mode in ((0, "Auto"), (3, "4"))]
    parent = SimpleNamespace(connect_timeout_s=0.1, poll_timeout_s=0.1,
                             on=True, getChannels=lambda: channels,
                             getConfiguredModules=lambda: [0, 3])
    parent.isOn = lambda: parent.on
    parent._set_on_ui_state = lambda value: setattr(parent, "on", value)
    controller = module.DMMRController(parent)
    controller.device = hardware = RangeDevice()
    controller.initialized = True
    controller.detected_module_ids = [0, 3]
    messages = []
    controller.print = lambda message, **kw: messages.append(message)
    return SimpleNamespace(module=module, controller=controller, hardware=hardware,
                           channels=channels, parent=parent, messages=messages)


class StartupDevice(RangeDevice):
    def __init__(self, recovery_class):
        super().__init__()
        self.recovery_class = recovery_class
        self.connected = True
        self._transport_poisoned = False
        self.baudrate = self.port_baud = 230400
        self.port_closes = 0
        self.modes = dict.fromkeys(range(8), False)
        self.ranges = dict.fromkeys(range(8), 0)
        self.faults, self.events, self.counts = {}, [], Counter()
        self.sticky = self.desync = self.persistent = False
        self.desync_keys = None
        self.poison_at = self.reject_write = None
        self.purge_status = 0
        self.purge_hook = lambda: None

    def new_command_recovery(self, **kw):
        return self.recovery_class(self, **kw)

    def record_protocol_event(self, event):
        self.events.append(json.loads(json.dumps(event)))

    def reply(self, key):
        self.counts[key] += 1
        if self.poison_at == key:
            self._transport_poisoned = True
        if key in self.faults and self.counts[key] == 1:
            if self.desync and (self.desync_keys is None or key in self.desync_keys):
                self.sticky = True
            return self.faults[key]
        return -13 if self.sticky else 0

    def set_module_auto_range(self, address, enabled, **kw):
        self.calls.append(('auto_range', address, enabled))
        if self.reject_write != ('auto_range', address):
            self.modes[address] = enabled
        return self.reply(('auto_range', address))

    def set_module_meas_range(self, address, value, **kw):
        self.calls.append(('fixed_range', address, value))
        if self.reject_write != ('fixed_range', address):
            self.ranges[address] = value
        return self.reply(('fixed_range', address))

    def get_module_meas_range(self, address, **kw):
        self.calls.append(('range_readback', address))
        return self.reply(('range_readback', address)), self.ranges[address], self.modes[address]

    def get_state(self, **kw):
        self.calls.append(('state',))
        return self.reply(('state',)), '0x0000', 'ST_ON'

    def set_enable(self, value, **kw):
        self.calls.append(('enable', value))
        self.enabled = value
        return self.reply(('enable', value))

    def set_automatic_current(self, value, **kw):
        self.calls.append(('automatic', value))
        self.automatic = value
        return self.reply(('automatic', value))

    def get_enable(self, **kw):
        self.calls.append(('read_enable',))
        return self.reply(('read_enable',)), self.enabled

    def get_automatic_current(self, **kw):
        self.calls.append(('read_automatic',))
        return self.reply(('read_automatic',)), self.automatic

    def _call_locked_with_timeout(self, method, timeout, name, *args):
        return method(*args)

    def purge(self):
        self.calls.append(('purge',))
        self.port_baud = 9600
        self.sticky = self.persistent
        self.purge_hook()
        return self.purge_status

    def set_baud_rate(self, baud):
        self.calls.append(('baud', baud))
        self.port_baud = baud
        return 0, baud

    def disconnect(self):
        if not self.connected:
            return True
        assert not self.enabled and not self.automatic
        self.calls.append(('disconnect',))
        self.connected = False
        self.port_closes += 1
        return True


@pytest.fixture
def dmmr_startup(dmmr_range_rig):
    recovery = dmmr_range_rig.module._get_dmmr_driver_class().CommandRecovery
    dmmr_range_rig.hardware = dmmr_range_rig.controller.device = StartupDevice(recovery)
    dmmr_range_rig.channels[:] = [SimpleNamespace(real=True, _requested_range_mode='Auto',
                                      module_address=lambda address=address: address)
                       for address in range(8)]
    dmmr_range_rig.parent.getConfiguredModules = lambda: list(range(8))
    dmmr_range_rig.controller.detected_module_ids = list(range(8))
    return dmmr_range_rig


DLL_PATH = Path(__file__).resolve().parents[2] / "dmmr/vendor/runtime/dmmr/vendor/x64/COM-DMMR-8.dll"


class NativeFunction:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = self.restype = None

    def __call__(self, *args):
        for kind, value in zip(self.argtypes, args):
            kind.from_param(value)
        return self.implementation(*args)


@pytest.fixture
def dmmr_native(tmp_path):
    cls = load_plugin()._get_dmmr_driver_class()._PROCESS_CONTROLLER_CLASS
    driver = cls.__new__(cls)
    driver.com, driver.baudrate = 13, 230400
    driver.connected = driver._dll_port_claimed = True
    driver._transport_poisoned = False
    driver._transport_error = None
    driver.thread_lock = threading.Lock()
    driver.logger = logging.getLogger("dmmr-startup-test")
    driver._set_port_claimed = lambda value: setattr(driver, '_dll_port_claimed', value)
    driver.dmmr_dll_path = DLL_PATH
    driver._startup_log_dir = tmp_path
    driver._startup_log_path = None
    calls = []
    state = SimpleNamespace(open_status=0, close_status=0, path=None)

    def open_log(filename):
        assert driver.thread_lock.locked()
        calls.append(("open", threading.get_ident()))
        state.path = Path(filename.decode("utf-8"))
        if state.open_status == 0:
            state.path.write_bytes(b"Opening debug file\r\n")
        return state.open_status

    def close_log():
        assert driver.thread_lock.locked()
        calls.append(("close", threading.get_ident()))
        # The actual DLL clears its FILE pointer even if fclose returns -401.
        return state.close_status

    driver.dll = SimpleNamespace(
        COM_DMMR_8_OpenDebugFile=NativeFunction(open_log),
        COM_DMMR_8_CloseDebugFile=NativeFunction(close_log),
    )
    driver._configure_dll_signatures()
    return SimpleNamespace(driver=driver, calls=calls, state=state, directory=tmp_path)


@pytest.fixture
def dmmr_connection(monkeypatch, tmp_path):
    plugin = load_plugin()
    cls = plugin._get_dmmr_driver_class()._PROCESS_CONTROLLER_CLASS
    base = importlib.import_module(cls.__module__).DMMRBase
    hw = types.SimpleNamespace(
        calls=[], opened=False, powered=False, release=threading.Event(),
        entered=threading.Event(), worker=None, late_status=0, block=True,
        close_status=0, close_release=threading.Event(), block_close=False,
        drivers=[],
    )

    def native_open(self, com):
        hw.calls.append('open')
        if hw.opened:
            return -2
        hw.opened = True
        hw.worker = threading.current_thread()
        hw.entered.set()
        if hw.block:
            assert hw.release.wait(5), 'test did not release simulated open'
            return hw.late_status
        return 0 if hw.powered else -5

    def native_close(self):
        # Closing is permitted only after the native open AND its wrapper exit.
        assert hw.worker is None or not hw.worker.is_alive()
        assert self.thread_lock.locked()
        hw.calls.append('close')
        if hw.block_close:
            assert hw.close_release.wait(5), 'test did not release simulated close'
        if hw.close_status == 0:
            hw.opened = False
        return hw.close_status

    monkeypatch.setattr(base, 'open_port', native_open)
    monkeypatch.setattr(base, 'close_port', native_close)
    monkeypatch.setattr(base, 'set_baud_rate', lambda self, rate: (0, rate))
    monkeypatch.setattr(base, 'get_io_state', lambda self: (0, 0))
    monkeypatch.setattr(base, 'get_comm_error', lambda self: (0, 0))

    def factory(**kwargs):
        driver = cls.__new__(cls)
        driver.device_id = 'retry-test'
        driver.com, driver.port_num, driver.baudrate = 15, 0, 230400
        driver.connected = driver._dll_port_claimed = driver._transport_poisoned = False
        driver._transport_error = None
        driver.err_dict = {}
        driver.thread_lock = threading.Lock()
        driver.logger = logging.getLogger('dmmr-retry-test')
        driver._startup_log_dir = tmp_path
        driver.stop_housekeeping = lambda: None
        driver._resolve_io_timeout = lambda timeout_s=None: 0.03 if timeout_s is None else timeout_s
        driver._warn_on_other_process_ports = lambda: None
        driver._verify_device_type = lambda timeout: driver.DEVICE_TYPE
        driver._warn_if_unexpected_product_id = lambda timeout: None
        driver.close = lambda: None  # Public facade cleanup, not a native close.
        def initialize(timeout_s):
            driver.connect(timeout_s=timeout_s)
            return {address: {} for address in range(8)}
        driver.initialize = initialize
        hw.drivers.append(driver)
        return driver

    monkeypatch.setattr(plugin, '_get_dmmr_driver_class', lambda: factory)
    parent = types.SimpleNamespace(
        name='DMMR', com=15, baudrate=230400, connect_timeout_s=0.03,
        poll_timeout_s=0.03, getChannels=lambda: [], isOn=lambda: False,
        pluginManager=types.SimpleNamespace(Settings=types.SimpleNamespace(dataPath=tmp_path)),
    )
    controller = plugin.DMMRController(parent)
    messages, successes = [], []
    controller.print = lambda message, **kwargs: messages.append(message)
    controller._restore_off_ui_state = lambda: None
    controller._sync_status_to_gui = lambda: None
    controller._update_state = lambda: True
    controller.signalComm = types.SimpleNamespace(
        initCompleteSignal=types.SimpleNamespace(emit=lambda: successes.append(True)),
    )
    hw.plugin = plugin
    hw.controller, hw.factory, hw.messages, hw.successes = controller, factory, messages, successes
    yield hw
    hw.release.set()
    hw.close_release.set()
    if hw.worker is not None:
        hw.worker.join(3)
    for driver in hw.drivers:
        pending = getattr(driver, '_pending_dll_call', None)
        if pending is not None:
            pending['thread'].join(3)
        driver._dll_port_claimed = False  # Drop this test's simulated handle only.
    with cls._active_connections_lock:
        cls._purge_stale_connections()


def finish_open(hw):
    hw.release.set()
    assert hw.entered.wait(2)
    hw.worker.join(3)
    assert not hw.worker.is_alive()
    hw.block = False
    hw.powered = True


class FaultingDMMR:
    NO_ERR = 0

    def __init__(self):
        self.enabled = False
        self.automatic = False
        self.calls = []
        self.fail_start = "range"
        self.auto_modes = {}
        self.start_failed = False
        self.reject_cleanup = False
        self.auto_disable_raises = False
        self.readback = {}
        self.state_error = False
        self.connected = True
        self.disconnect_result = True
        self.disconnect_error = None
        self.port_closes = 0

    def disconnect(self):
        self.calls.append(("disconnect",))
        if self.disconnect_error is not None:
            raise self.disconnect_error
        if self.disconnect_result is True and self.connected:
            self.connected = False
            self.port_closes += 1
        return self.disconnect_result

    def set_enable(self, value, **kwargs):
        self.calls.append(("enable", value))
        if not value and self.reject_cleanup:
            return -13
        self.enabled = value
        if value and self.fail_start == "enable_ack":
            self.start_failed = True
            return -12  # Hardware changed, but the acknowledgement was lost.
        return 0

    def set_automatic_current(self, value, **kwargs):
        self.calls.append(("automatic", value))
        if self.auto_disable_raises:
            raise RuntimeError("automatic command failed")
        if self.start_failed and self.reject_cleanup:
            return -13
        self.automatic = value
        return 0

    def set_module_auto_range(self, address, value, **kwargs):
        self.calls.append(("range", address, value))
        if address == 5 and self.fail_start == "range":
            self.start_failed = True
            return -12
        self.auto_modes[address] = value
        return 0

    def get_module_meas_range(self, address, **kwargs):
        self.calls.append(("range_readback", address))
        return self.NO_ERR, 0, self.auto_modes.get(address, False)

    def _get_gate(self, name, value):
        self.calls.append(("read", name))
        result = self.readback.get(name, (0, value))
        if isinstance(result, Exception):
            raise result
        return result

    def get_enable(self, **kwargs):
        return self._get_gate("enable", self.enabled)

    def get_automatic_current(self, **kwargs):
        return self._get_gate("automatic", self.automatic)

    def get_state(self, **kwargs):
        return (-13 if self.state_error else 0), "0x0000", "ST_ON"

    def format_status(self, status):
        return str(status)


@pytest.fixture
def dmmr_toggle_rig():
    module = load_plugin()
    states, logs = [], []
    parent = SimpleNamespace(
        connect_timeout_s=0.1, poll_timeout_s=0.1, on=True,
        getConfiguredModules=lambda: list(range(8)),
        getChannels=lambda: [SimpleNamespace(real=True, module_address=lambda: 5)],
    )
    parent.isOn = lambda: parent.on

    def set_ui(value):
        states.append(value)
        parent.on = value

    parent._set_on_ui_state = set_ui
    controller = module.DMMRController(parent)
    controller.device = FaultingDMMR()
    controller.initialized = True
    controller.detected_module_ids = list(range(8))
    controller.values = {5: 12e-12}
    controller.print = lambda message, **kwargs: logs.append(message)
    return SimpleNamespace(module=module, controller=controller, device=controller.device,
                           parent=parent, states=states, logs=logs)


def start(rig):
    assert rig.controller._begin_transition(True)
    rig.controller.toggleOn()
    assert rig.controller.transitioning is False
    assert rig.controller.transition_target_on is None


@pytest.fixture
def dmmr_failed_open(dmmr_connection, monkeypatch):
    hw = dmmr_connection
    p, c, mod = hw.controller.controllerParent, hw.controller, hw.plugin
    p.controller = c
    p.useOnOffLogic, p.loading = True, False
    p.onAction = SimpleNamespace(state=False)
    p.isOn = lambda: p.onAction.state
    p._set_on_ui_state = lambda on: setattr(p.onAction, 'state', on)
    p.stopAcquisition, p.print = c.stopAcquisition, c.print
    for name in ('_sync_acquisition_controls', '_sync_local_on_action',
                 '_sync_toolbar_communication_controls', '_update_status_widgets'):
        setattr(p, name, lambda: None)
    for name in ('closeCommunication', 'shutdownCommunication', 'setOn'):
        setattr(p, name, MethodType(getattr(mod.DMMRDevice, name), p))
    for name in ('_restore_off_ui_state', '_restore_on_ui_state', '_update_state'):
        setattr(c, name, MethodType(getattr(mod.DMMRController, name), c))
    c.toggleOnFromThread = lambda **kwargs: c.toggleOn()
    hw.runtime_calls = []
    factory = hw.factory

    def tracked_factory(**kwargs):
        d = factory(**kwargs)
        for name in ('shutdown', 'get_state', 'get_device_state', 'get_voltage_state',
                     'get_temperature_state', 'set_enable', 'set_automatic_current'):
            original = getattr(d, name)

            def tracked(*args, _method=original, _name=name, **kw):
                hw.runtime_calls.append(_name)
                return _method(*args, **kw)

            setattr(d, name, tracked)
        return d

    monkeypatch.setattr(mod, '_get_dmmr_driver_class', lambda: tracked_factory)
    return hw


ACTIONS = ('controller_close', 'device_close', 'controller_shutdown',
           'device_shutdown', 'controller_off', 'device_off')


def request(hw, action):
    c, p = hw.controller, hw.controller.controllerParent
    if action.endswith('_off'):
        p.onAction.state = False
        if action == 'device_off':
            return p.setOn(on=False)
        assert c._begin_transition(False)
        return c.toggleOn()
    owner = p if action.startswith('device_') else c
    return getattr(owner, 'shutdownCommunication' if action.endswith('shutdown') else 'closeCommunication')()


def inject_stream_fault(device, *, status=-13, persistent=False, sticky=False, malformed=False, after_s=None):
    original = device.get_current
    original_state, original_purge = device.get_state, device.purge
    faulted = False
    desynchronized = False

    def frame(**kw):
        nonlocal faulted, desynchronized
        due = device.clock() >= after_s if after_s is not None else device.frames >= 32
        if device.automatic and due and (not faulted or persistent):
            faulted = True
            desynchronized = sticky
            device.clock.sleep(.01)
            return status, 0, 666., 0, 1e6
        return original(**kw)

    def state(**kw):
        if desynchronized:
            return -13, '0x0000', 'ST_ON'
        if malformed and faulted and device.automatic:
            return 0, '0x0001', 'ST_ERR'
        return original_state(**kw)

    def purge():
        nonlocal desynchronized
        desynchronized = False
        device.clock.sleep(.15)
        return original_purge()

    device.get_current, device.get_state, device.purge = frame, state, purge


def wire_native(driver, hardware):
    """Keep the actual locks, timeout wrapper, diagnostics and ACK workaround."""
    driver.baudrate = hardware.baudrate
    driver.err_dict = {}
    driver.hk_running = False
    driver.device_id = 'DMMR-native-startup-test'
    driver._optional_command_support = {}
    driver._optional_command_warnings = set()
    last_error = 0

    def bind(export, function):
        def checked(*args):
            nonlocal last_error
            assert driver.thread_lock.locked(), export
            status = function(*args)
            if status < 0:
                last_error = status
            return status
        setattr(driver.dll, 'COM_DMMR_8_' + export, NativeFunction(checked))

    def value(arg):
        return getattr(arg, 'value', arg)

    def flag(getter, pointer):
        status, result = getter()
        pointer._obj.value = result
        return status

    def state(pointer):
        status, result, _ = hardware.get_state()
        pointer._obj.value = int(result, 16)
        return status

    def ranges(address, meas_range, auto):
        status, result, enabled = hardware.get_module_meas_range(value(address))
        meas_range._obj.value, auto._obj.value = result, enabled
        return status

    def baud(pointer):
        status, result = hardware.set_baud_rate(pointer._obj.value)
        pointer._obj.value = result
        return status

    def io(pointer):
        hardware.calls.append(('io',))
        pointer._obj.value = last_error
        return 0

    def comm(pointer):
        hardware.calls.append(('comm',))
        pointer._obj.value = 0
        return 0

    bind('SetEnable', lambda enabled: hardware.set_enable(value(enabled)))
    bind('GetEnable', lambda pointer: flag(hardware.get_enable, pointer))
    bind('SetAutomaticCurent', lambda enabled: hardware.set_automatic_current(value(enabled)))
    bind('GetAutomaticCurent', lambda pointer: flag(hardware.get_automatic_current, pointer))
    bind('SetModuleAutoRange', lambda address, enabled: hardware.set_module_auto_range(value(address), value(enabled)))
    bind('SetModuleMeasRange', lambda address, meas_range: hardware.set_module_meas_range(value(address), value(meas_range)))
    bind('GetModuleMeasRange', ranges)
    bind('GetState', state)
    bind('Purge', hardware.purge)
    bind('Close', lambda: 0 if hardware.disconnect() else -1)
    bind('SetBaudRate', baud)
    bind('GetIOState', io)
    bind('GetCommError', comm)
    driver._configure_dll_signatures()


@pytest.fixture
def dmmr_history_rig(tmp_path, monkeypatch):
    h5py = pytest.importorskip("h5py")
    host = Path(distribution("esibd-explorer").locate_file("esibd"))
    clock = SimpleNamespace(now=0.0)
    ns = dict(np=np, Path=Path, h5py=h5py, cast=lambda kind, value: value,
              time=SimpleNamespace(time=lambda: clock.now),
              INOUT=SimpleNamespace(IN=1, OUT=2), INPUTCHANNELS="Input Channels",
              OUTPUTCHANNELS="Output Channels", UNIT="Unit")

    def extract(file, name, parent=None):
        exec(compile_definition(host / file, name, parent), ns)
        return ns[name]

    buffer = extract("core.py", "DynamicNp")
    host_append = extract("core.py", "appendValue", "Channel")
    host_clear = extract("core.py", "clearHistory", "Channel")
    append_data = extract("plugins.py", "appendData", "Device")
    exporter = extract("plugins.py", "appendOutputData", "Device")
    restore = extract("plugins.py", "restoreOutputData", "Device")
    estimate = extract("plugins.py", "estimateStorage", "Device")
    module = load_plugin()
    ns["PRINT"] = module.PRINT
    monkeypatch.setattr(module.Channel, "appendValue", host_append, raising=False)
    monkeypatch.setattr(module.Channel, "clearHistory", host_clear, raising=False)
    if "def clearHistoryInternal" in (host / "core.py").read_text():  # Explorer 1.0.1
        monkeypatch.setattr(module.Channel, "clearHistoryInternal",
                            extract("core.py", "clearHistoryInternal", "Channel"), raising=False)
    monkeypatch.setattr(module.Channel, "getRecordedParameters", lambda self: [], raising=False)
    monkeypatch.setattr(module.Device, "appendOutputData", exporter, raising=False)
    monkeypatch.setattr(module.Device, "restoreOutputData", restore, raising=False)
    monkeypatch.setattr(module.Device, "estimateStorage", estimate, raising=False)
    monkeypatch.setattr(sys.modules["esibd.core"], "DynamicNp", buffer, raising=False)
    const = ModuleType("esibd.const")
    const.INPUTCHANNELS, const.OUTPUTCHANNELS = ns["INPUTCHANNELS"], ns["OUTPUTCHANNELS"]
    monkeypatch.setitem(sys.modules, "esibd.const", const)
    device = module.DMMRDevice.__new__(module.DMMRDevice)
    device.channels = []
    device.time = buffer(dtype=np.float64, max_size=1000)
    device.defaultChannel = SimpleNamespace(getRecordedParameters=lambda: [])  # Explorer 1.0.1
    device.maxDataPoints = 1000
    device.MAXDATAPOINTS = "Max data points"
    device.maxStorage = 50
    device.interval = 1000
    device.TIME = "Time"
    device.confh5 = "_DMMR.h5"
    device.useBackgrounds = False
    device.pluginManager = SimpleNamespace(Settings=SimpleNamespace(configPath=tmp_path, loading=False),
                                           Device=module.DMMRDevice, DeviceManager=True)
    device.pluginManager.Settings.settings = {
        "DMMR/Max data points": SimpleNamespace(getWidget=lambda: None),
    }
    device.getChannels = device.getDataChannels = lambda: device.channels
    device.getChannelByName = lambda name: next((ch for ch in device.channels if ch.name == name), None)
    device.liveDisplay = SimpleNamespace(livePlotWidgets=[])
    device.requireGroup = lambda owner, name: owner.require_group(name)
    device.plotableChannels = True
    device.updateValues = lambda: None
    device.liveDisplayActive = lambda: False
    device.measureInterval = lambda: None
    messages = []
    device.print = lambda msg, **kw: messages.append(msg)

    def add_channel():
        ch = module.DMMRChannel.__new__(module.DMMRChannel)
        ch.channelParent = device
        ch.pluginManager = device.pluginManager
        ch.module = len(device.channels)
        ch.name = f"DMMR_M{ch.module:02}"
        ch.real = ch.enabled = ch.useMonitors = True
        ch.useBackgrounds = False
        ch.inout = 1
        ch.value = 999.0  # Never record the unused reference when Read is muted.
        ch.monitor = np.nan
        ch.clearPlotCurve = lambda: None
        ch.values = buffer(max_size=device.maxDataPoints)
        device.channels.append(ch)
        return ch

    def record(count, *, missing=(), gap=False):
        for _ in range(count):
            clock.now += 1
            token = object()
            for i, ch in enumerate(device.channels):
                ch.monitor = np.nan if clock.now in missing else (clock.now + i * 1000) * 1e-12
                ch.measurement_range = int(clock.now + i) % 5
                ch._measurement_token = token
            append_data(device, nan=gap)

    def small_limit(limit):
        device.maxDataPoints = device.time.max_size = limit
        for ch in device.channels:
            ch.values.max_size = ch._range_buffer().max_size = limit

    def save(name="DMMR.h5", default=True):
        file = tmp_path / name
        with h5py.File(file, "w") as data:
            device.appendOutputData(data, useDefaultFile=default)
        return file

    return SimpleNamespace(module=module, device=device, clock=clock, buffer=buffer,
                           record=record, add=add_channel, small_limit=small_limit,
                           append=append_data, host_append=host_append, save=save,
                           h5py=h5py, tmp=tmp_path, messages=messages)


def host_device_close(parent):
    """Execute the real host close flow, with no record/export or outer dock to destroy."""
    source = os.environ.get('ESIBD_EXPLORER_SOURCE')
    path = (Path(source) / 'esibd/plugins.py' if source else
            Path(distribution('esibd-explorer').locate_file('esibd/plugins.py')))
    tree = ast.parse(path.read_text(encoding='utf-8'))
    device = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Device')
    close = next(node for node in device.body if isinstance(node, ast.FunctionDef) and node.name == 'close')
    class Base:
        def close(self):
            return True
    ns = {'Base': Base}
    exec(compile('class HostDevice(Base):\n' + '\n'.join('    ' + line for line in ast.unparse(close).splitlines()),
                 str(path), 'exec'), ns)
    wrapper = ns['HostDevice']()
    wrapper.initialized = parent.controller.initialized
    wrapper.hasRecorded = False  # Open never succeeded: there is no acquired history.
    wrapper.closeCommunication = parent.closeCommunication
    return wrapper.close()


def use_real_label_parameters(module, device):
    """Use Explorer's real Parameter widget/getter/setter, without its app shell."""
    import ast
    from enum import Enum
    from importlib.metadata import distribution
    import re

    import numpy as np
    from PyQt6 import QtCore, QtGui, QtWidgets

    path = Path(distribution("esibd-explorer").locate_file("esibd/core.py"))
    tree = ast.parse(path.read_text())
    ns = dict(np=np, re=re, Path=Path, cast=lambda _, value: value, Channel=module.Channel, pyqtSignal=QtCore.pyqtSignal,
              PARAMETERTYPE=Enum("PARAMETERTYPE", "COMBO INTCOMBO FLOATCOMBO TEXT INT FLOAT EXP BOOL COLOR LABEL PATH"))
    for qt in (QtCore, QtGui, QtWidgets):
        ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith("Q")})
    ns.update(CheckBox=QtWidgets.QCheckBox, CompactComboBox=QtWidgets.QComboBox,
              LedIndicator=QtWidgets.QAbstractButton, PRINT=module.PRINT)
    for name in ("ParameterWidget", "Label", "LabviewSpinBox", "LabviewDoubleSpinBox", "LabviewSciSpinBox", "LineEdit", "Parameter", "parameterDict"):
        node = next(n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name)
        unit = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(unit), str(path), "exec"), ns)
    module.Parameter = ns["Parameter"]
    module.PARAMETERTYPE = ns["PARAMETERTYPE"]
    module.parameterDict = ns["parameterDict"]
    module.Channel.getDefaultChannel = lambda self: {key: {} for key in ("Value", "Enabled", "Display", "Active")}
    module.Channel.displayChanged = lambda self: None
    declaration = module.DMMRChannel.__new__(module.DMMRChannel).getDefaultChannel()["Label"]
    module.DMMRChannel.label = property(lambda ch: ch.label_parameter.value,
                                       lambda ch, value: setattr(ch.label_parameter, "value", value))
    for i, previous in enumerate(device.channels):
        channel = module.DMMRChannel.__new__(module.DMMRChannel)
        channel.__dict__.update({k: v for k, v in vars(previous).items() if k != "label"})
        channel.channelParent = device
        device.loading = True  # Channel.loading is read-only: it follows the device.
        channel.print = device.print
        channel.tree, channel.rowHeight = None, 28
        parameter = ns["Parameter"]("Label", channel, parameterType=declaration[ns["Parameter"].PARAMETER_TYPE])
        channel.label_parameter = parameter
        parameter.value = previous.label
        device.loading = False
        device.channels[i] = channel


def hold_host_initialization(hw, monkeypatch):
    """Use the real Explorer initializer, holding only Thread.start for the test."""
    source = os.environ.get('ESIBD_EXPLORER_SOURCE')
    path = (Path(source) / 'esibd/core.py' if source else
            Path(distribution('esibd-explorer').locate_file('esibd/core.py')))
    tree = ast.parse(path.read_text(encoding='utf-8'))
    controller = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DeviceController')
    method = next(n for n in controller.body if isinstance(n, ast.FunctionDef) and n.name == 'initializeCommunication')
    queued = []

    class HeldThread:
        def __init__(self, *, target, name):
            self.target, self.name = target, name
        def start(self):
            queued.append(self.target)

    ns = {'Thread': HeldThread, 'getTestMode': lambda: False, 'PRINT': SimpleNamespace(DEBUG=None)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), ns)
    base = hw.plugin.DMMRController.__mro__[1]
    monkeypatch.setattr(base, 'initializeCommunication', ns['initializeCommunication'], raising=False)
    hw.controller.acquisitionThread = None
    return queued
