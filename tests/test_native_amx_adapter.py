"""Native AMX transport retirement is not hardware OFF confirmation."""

from __future__ import annotations

from collections import deque
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock, Thread
import time
from types import SimpleNamespace

import pytest

from test_amx_config_load import load_module
from test_native_worker import binary  # noqa: F401

FOLDERS = ("amx_a", "amx_hd")


class NativeProxy:
    """Supervisor double behind the real public facade, never a Python DLL."""

    def __init__(self, family):
        self.family = family
        self.session = f"native-{family}-adapter-test"
        self.closed = False
        self.connected = False
        self.claimed = False
        self.opening = False
        self.enabled = True  # Hardware may already run when Explorer connects.
        self.calls = []
        self.tokens = []
        self.failures = {}
        self.on_call = None
        self.blocked_method = None
        self.entered = Event()
        self.release = Event()
        self.close_result = True
        self.close_calls = 0
        self.shutdown_result = True

    def lifecycle_attribute(self, name):
        return {
            "connected": self.connected and not self.closed,
            "_dll_port_claimed": self.claimed,
            "_opening_in_progress": self.opening,
            "_open_failed": False, "_failed_open_released": False,
            "_transport_poisoned": self.closed,
            "_transport_error": "mock worker failed" if self.closed else None,
        }[name]

    def get_attribute(self, name, **kwargs):
        assert not self.closed, "attribute RPC was sent to a dead worker"
        constants = {"CLOCK": 100e6, "OSC_OFFSET": 2, "PULSER_WIDTH_OFFSET": 2,
                     "PULSER_DELAY_OFFSET": 3, "_process_backend_disabled_reason": ""}
        if name in constants:
            return constants[name]
        raise AttributeError(name)

    def snapshot(self):
        return dict(
            main_state={"name": "STATE_ON" if self.enabled else "STATE_STANDBY"},
            device_enabled=self.enabled, device_state={"flags": []}, controller_state={"flags": []},
            oscillator={"period": 198, "frequency_hz": 500e3},
            pulsers=[dict(pulser=p, width_ticks=98 if p == 0 else 0, delay_ticks=7, burst=0)
                     for p in range(2 if self.family == "amx_hd" else 4)],
        )

    def call_method(self, method, *args, rpc_timeout_s=30., _cancel_event=None, _io_timeout_s=None, **kwargs):
        assert not self.closed, "method RPC was sent to a dead worker"
        # Events and Python callbacks must be consumed client-side, not encoded.
        json.dumps([args, kwargs], allow_nan=False)
        self.calls.append((method, args, kwargs, rpc_timeout_s))
        self.tokens.append(_cancel_event)
        if method == "connect":
            self.opening = True
        if method == self.blocked_method:
            self.entered.set()
            deadline = time.monotonic() + 2.
            while not self.release.wait(.01):
                if _cancel_event is not None and _cancel_event.is_set():
                    self.closed = True
                    self.opening = False
                    raise RuntimeError("Cancelled native operation remained blocked; worker terminated")
                assert time.monotonic() < deadline, "test did not cancel/release the blocked RPC"
        if method in self.failures:
            failure, close_worker = self.failures[method]
            self.closed = close_worker
            self.opening = False
            raise failure
        result = None
        if method == "connect":
            self.connected = self.claimed = True
            self.opening = False
            result = True
        elif method == "list_configs":
            result = [dict(index=79, name="Stored signal", active=True, valid=True)]
        elif method == "get_status":
            result = dict(connected=self.connected, memory_config=79,
                          memory_config_name="Stored signal", memory_config_source="memory")
        elif method in {"collect_housekeeping", "collect_state_snapshot"}:
            result = self.snapshot()
        elif method == "set_device_enabled":
            self.enabled = bool(args[0])
        elif method == "load_config":
            assert args == (79,)
        elif method == "shutdown":
            result = self.shutdown_result
            if result is True:
                self.enabled = self.connected = self.claimed = False
        elif method == "disconnect":
            self.connected = self.claimed = False
            result = True
        elif method not in {"set_frequency_khz", "set_pulser_width_ticks"}:
            raise AssertionError(f"unexpected native operation: {method}")
        if self.on_call is not None:
            self.on_call(method)
        return result

    def close(self, *, grace_s=None):
        self.close_calls += 1
        if self.close_result:
            self.closed = True
        return self.close_result


def make_case(folder, tmp_path, monkeypatch, *, direct_gui=True):
    module = load_module(folder)
    if direct_gui:
        monkeypatch.setattr(module, "_invoke_gui_callback", lambda callback: callback())
    hd = folder == "amx_hd"
    device_cls = module.AMXHDDevice if hd else module.AMXDevice
    parent = device_cls.__new__(device_cls)
    parent.name = "AMX_HD" if hd else "AMX_A"
    parent.com, parent.baudrate = 7, 230400
    parent.connect_timeout_s = parent.poll_timeout_s = parent.startup_timeout_s = .2
    parent.frequency_khz, parent.operating_config, parent.standby_config = 2., 79, -1
    parent.pluginManager = SimpleNamespace(Settings=SimpleNamespace(dataPath=tmp_path))
    parent.loading = False
    parent.useOnOffLogic = True
    parent.onAction = SimpleNamespace(state=True)
    parent.deviceOnAction = SimpleNamespace(state=True)
    parent.isOn = lambda: parent.onAction.state
    parent.recording = True
    parent.channels = [SimpleNamespace(
        real=True, enabled=True, active=True, value=1., id=p,
        pulser_number=lambda p=p: p, getParameterByName=lambda _: None,
        setWidthText=lambda _: None, setDelayText=lambda _: None, setBurstText=lambda _: None,
        setDutyText=lambda _: None, setFreqText=lambda _: None,
    ) for p in range(2 if hd else 4)]
    parent.getChannels = lambda: parent.channels
    messages, successes, resumes = [], [], []
    completions = deque()
    parent.print = lambda message, **kwargs: messages.append(message)
    parent._sync_channels = lambda: None
    parent._sync_acquisition_controls = lambda: None
    parent._sync_toolbar_communication_controls = lambda: None
    parent._update_config_controls = lambda: None
    parent._update_status_widgets = lambda: None
    parent._finish_setpoint_edits = lambda: None
    parent._sync_local_on_action = lambda: setattr(parent.deviceOnAction, "state", parent.onAction.state)
    controller = parent.controller = (module.AMXHDController if hd else module.AMXController)(parent)
    controller.lock = Lock()
    controller.errorCount = 0
    controller.print = parent.print
    controller.initializing = controller.initialized = controller.acquiring = False
    controller.signalComm = SimpleNamespace(
        initCompleteSignal=SimpleNamespace(emit=lambda: successes.append(True)),
        updateValuesSignal=SimpleNamespace(emit=controller.updateValues),
    )
    controller.toggleOnFromThread = lambda **kwargs: resumes.append(kwargs)
    controller._restart_acquisition_after_transition = lambda: resumes.append("acquire")
    init_complete = controller.initComplete

    def receive_init_complete(**kwargs):
        if kwargs:
            successes.append(True)
            completions.append(lambda: init_complete(**kwargs))
        else:
            init_complete()

    monkeypatch.setattr(controller, "initComplete", receive_init_complete)

    def launch(self):
        self.initializing = True
        self.runInitialization()

    monkeypatch.setattr(module.DeviceController, "initializeCommunication", launch, raising=False)
    parent.initializeCommunication = controller.initializeCommunication
    facade_cls = module._get_amx_driver_class()
    proxies = deque([NativeProxy("amx_hd" if hd else "amx")])
    created, constructor_kwargs = [], []

    def factory(**kwargs):
        constructor_kwargs.append(kwargs)
        facade = facade_cls.__new__(facade_cls)
        object.__setattr__(facade, "_backend_mode", "process")
        object.__setattr__(facade, "_backend", proxies.popleft())
        object.__setattr__(facade, "_process_backend_disabled_reason", "")
        created.append(facade)
        return facade

    monkeypatch.setattr(module, "_get_amx_driver_class", lambda: factory)
    return SimpleNamespace(module=module, parent=parent, controller=controller, proxies=proxies,
                           created=created, constructor_kwargs=constructor_kwargs,
                           messages=messages, successes=successes, resumes=resumes,
                           completions=completions)


@pytest.fixture(params=FOLDERS)
def case(request, tmp_path, monkeypatch):
    return make_case(request.param, tmp_path, monkeypatch)


def start(case):
    proxy = case.proxies[0]
    case.controller.initializeCommunication()
    case.parent._set_on_ui_state(False)
    case.completions.popleft()()
    case.parent._set_on_ui_state(True)
    case.controller.acquiring = True
    return proxy


def assert_lost(case):
    controller, parent = case.controller, case.parent
    assert controller.main_state == parent.main_state == "Shutdown unconfirmed"
    assert controller.device_enabled_state == parent.device_enabled_state == "Unknown"
    assert not parent.onAction.state and not parent.deviceOnAction.state
    assert not controller.initialized and not controller.acquiring
    assert getattr(controller, "output_rows", None) is None
    assert not parent.recording
    assert all(math.isnan(value) for value in controller.values.values())
    assert controller.available_configs == [] and controller._loaded_config_index == -1
    assert any("Output safety is not confirmed" in message for message in case.messages)


def test_constructor_requires_native_backend_and_data_path_logs_without_output_commands(case):
    proxy = case.proxies[0]
    case.controller.initializeCommunication()
    assert case.constructor_kwargs == [dict(
        device_id=f"{case.parent.name.lower()}_com7", com=7, baudrate=230400,
        native_backend=True,
        log_dir=Path(case.parent.pluginManager.Settings.dataPath) / "logs" / case.parent.name.lower(),
    )]
    assert [call[0] for call in proxy.calls] == ["connect", "list_configs", "get_status", "collect_housekeeping"]
    assert case.successes == [True] and case.resumes == []
    assert proxy.enabled, "transport connection must not silently change running hardware"


@pytest.mark.parametrize("fault", [RuntimeError, TimeoutError])
@pytest.mark.parametrize("method", ["connect", "list_configs", "collect_housekeeping"])
def test_initialization_failure_retires_worker_and_fresh_on_is_an_explicit_request(case, fault, method):
    old = case.proxies[0]
    old.failures[method] = (fault("native worker failed"), True)
    case.controller.initializeCommunication()
    assert_lost(case)
    assert case.controller.device is None and old.close_calls >= 1
    assert old.enabled and case.successes == [] and case.resumes == []
    fresh = NativeProxy(old.family)
    case.proxies.append(fresh)
    case.parent.setOn(True)
    assert case.controller.device is case.created[-1]
    assert not case.controller._native_cancel_event.is_set()
    assert case.successes == [True] and case.resumes == []
    case.completions.popleft()()
    assert case.resumes == [{"parallel": True}]
    assert not any(call[0] in {"set_device_enabled", "load_config"} for call in fresh.calls)


@pytest.mark.parametrize("fault", [RuntimeError, TimeoutError])
def test_dead_poll_forces_request_off_immediately_but_shutdown_stays_unconfirmed(case, fault):
    proxy = start(case)
    proxy.failures["collect_housekeeping"] = (fault("native worker failed"), True)
    case.controller.readNumbers()
    assert_lost(case)
    assert case.controller.device is None and case.controller._consecutive_poll_errors == 1
    before = list(proxy.calls)
    case.parent.shutdownCommunication()
    case.controller._restore_on_ui_state()
    assert_lost(case)
    assert proxy.calls == before and proxy.enabled


def test_alive_worker_vendor_read_error_is_not_misclassified_as_a_crash(case):
    proxy = start(case)
    proxy.failures["collect_housekeeping"] = (RuntimeError("vendor read failed: -4"), False)
    case.controller.readNumbers()
    assert case.controller.device is case.created[0] and case.controller.initialized
    assert case.parent.onAction.state and case.parent.deviceOnAction.state
    assert not proxy.closed and proxy.close_calls == 0
    assert case.controller._consecutive_poll_errors == 1


def test_initial_state_refresh_does_not_hide_native_timeout_as_a_busy_lock(case):
    proxy = start(case)
    proxy.failures["collect_housekeeping"] = (TimeoutError("DLL watchdog expired"), True)
    with pytest.raises(TimeoutError):
        case.controller._update_state()
    assert_lost(case)


@pytest.mark.parametrize("result", [False, RuntimeError("OFF readback remained enabled")])
def test_alive_unconfirmed_off_retains_worker_and_refuses_bare_reinitialization(case, result):
    proxy = start(case)
    if isinstance(result, Exception):
        proxy.failures["shutdown"] = (result, False)
    else:
        proxy.shutdown_result = result
    case.parent.shutdownCommunication()
    assert case.controller.main_state == "Shutdown unconfirmed"
    assert case.parent.onAction.state and case.parent.deviceOnAction.state
    assert case.controller.device is case.created[0] and proxy.enabled
    before = list(proxy.calls)
    case.controller.initializeCommunication()
    assert proxy.calls == before and len(case.created) == 1
    proxy.failures.clear()
    proxy.shutdown_result = True
    case.parent.shutdownCommunication()
    assert case.controller.main_state == "Disconnected" and case.controller.device is None
    assert not proxy.enabled and not case.parent.onAction.state
    assert [call[0] for call in proxy.calls].count("shutdown") == 2
    assert not any(call[0] == "disconnect" for call in proxy.calls)


def test_failed_worker_retirement_does_not_allow_replacement_until_reaped(case):
    proxy = start(case)
    proxy.closed = True
    proxy.close_result = False
    case.controller.readNumbers()
    assert_lost(case)
    assert case.controller.device is case.created[0]
    case.controller.initializeCommunication()
    assert len(case.created) == 1
    proxy.close_result = True
    case.proxies.append(NativeProxy(proxy.family))
    case.controller.initializeCommunication()
    assert len(case.created) == 2 and case.controller.device is case.created[1]


def test_off_cancels_blocked_connect_locally_and_never_starts_a_competing_shutdown(case):
    proxy = case.proxies[0]
    proxy.blocked_method = "connect"
    worker = Thread(target=case.controller.initializeCommunication)
    worker.start()
    try:
        assert proxy.entered.wait(1.)
        assert case.controller._initial_open_incomplete()
        started = time.monotonic()
        case.parent.setOn(False)
        assert time.monotonic() - started < .2
        assert case.controller._native_cancel_event.is_set()
        assert not case.parent.onAction.state and not case.parent.deviceOnAction.state
    finally:
        case.controller._native_cancel_event.set()
        worker.join(3.)
        assert not worker.is_alive()
    assert_lost(case)
    assert [call[0] for call in proxy.calls] == ["connect"]
    assert case.successes == [] and proxy.enabled


def test_shutdown_cancels_a_blocked_poll_without_asserting_hardware_off(case):
    proxy = start(case)
    proxy.blocked_method = "collect_housekeeping"
    worker = Thread(target=case.controller.readNumbers)
    worker.start()
    try:
        assert proxy.entered.wait(1.)
        case.parent.shutdownCommunication()
    finally:
        case.controller._native_cancel_event.set()
        worker.join(3.)
        assert not worker.is_alive()
    assert_lost(case)
    assert not any(call[0] in {"shutdown", "disconnect"} for call in proxy.calls)
    assert proxy.enabled


def test_off_arriving_with_successful_connect_reply_uses_verified_shutdown_not_bare_close(case):
    proxy = case.proxies[0]
    proxy.on_call = lambda method: case.parent.setOn(False) if method == "connect" else None
    case.controller.initializeCommunication()
    assert [call[0] for call in proxy.calls] == ["connect", "shutdown"]
    assert not proxy.enabled and case.controller.device is None
    assert case.controller.main_state == "Disconnected" and case.successes == []
    assert not case.parent.onAction.state and not case.parent.deviceOnAction.state
    assert proxy.tokens[-1] is None, "OFF verification must not reuse the cancelled token"


@pytest.mark.parametrize("method", ["load_config", "set_device_enabled"])
def test_off_during_startup_cancels_locally_and_confirms_shutdown_before_disconnect(case, method):
    proxy = start(case)
    case.controller.toggleOnFromThread = lambda **kwargs: case.controller.toggleOn()
    proxy.on_call = lambda name: case.parent.setOn(False) if name == method else None
    case.parent.setOn(True)
    assert not proxy.enabled and case.controller.device is None
    assert case.controller.main_state == "Disconnected"
    assert not case.parent.onAction.state and not case.parent.deviceOnAction.state
    assert any(call[0] == "shutdown" for call in proxy.calls)
    assert not any("timing enabled" in message for message in case.messages)
    assert proxy.tokens[-1] is None


def test_queued_init_success_cannot_revive_a_worker_that_has_crashed(case):
    proxy = case.proxies[0]
    case.controller.initializeCommunication()
    proxy.closed = True
    case.completions.popleft()()
    assert_lost(case)
    assert case.resumes == []


def test_queued_native_init_success_cannot_initialize_or_activate_a_replacement(case):
    old = case.proxies[0]
    case.controller.initializeCommunication()
    old_completion = case.completions.popleft()
    old.closed = True
    case.controller.closeCommunication()
    assert_lost(case)
    fresh = NativeProxy(old.family)
    case.proxies.append(fresh)
    case.controller.initializeCommunication()
    assert case.controller._native_ready_device is case.created[-1]
    assert not case.controller.initialized
    case.parent._set_on_ui_state(True)
    old_completion()
    assert not case.controller.initialized and not case.resumes
    case.completions.popleft()()
    assert case.controller.initialized and case.resumes == [{"parallel": True}]
    old_completion()
    assert case.resumes == [{"parallel": True}]


def test_parameterless_native_init_success_cannot_complete_a_new_ready_session(case):
    case.controller.initializeCommunication()
    assert case.controller._native_ready_device is case.controller.device
    case.controller.initComplete()
    assert not case.controller.initialized and not case.resumes
    case.completions.popleft()()
    assert case.controller.initialized and case.resumes == [{"parallel": True}]
    case.controller.initComplete()
    assert case.resumes == [{"parallel": True}]


def test_old_button_callback_cannot_clear_a_new_explicit_session(case, monkeypatch):
    callbacks = []
    monkeypatch.setattr(case.module, "_invoke_gui_callback", callbacks.append)
    case.controller._restore_off_ui_state()
    case.controller._native_cancel_event = Event()
    for callback in callbacks:
        callback()
    assert case.parent.onAction.state and case.parent.deviceOnAction.state


def test_queued_on_callback_cannot_reactivate_after_native_retirement(case, monkeypatch):
    proxy = start(case)
    callbacks = []
    monkeypatch.setattr(case.module, "_invoke_gui_callback", callbacks.append)
    case.controller._restore_on_ui_state()
    proxy.closed = True
    case.controller.readNumbers()
    while callbacks:
        callbacks.pop(0)()
    assert_lost(case)


def test_real_native_process_crash_does_not_retain_on_and_leaves_other_worker_alive(case, binary, tmp_path):
    from test_native_device_recovery import NativeDevice, proxy

    controller = case.controller
    family = "amx_hd" if case.parent.name == "AMX_HD" else "amx"
    worker, other = proxy(binary, tmp_path, family), proxy(binary, tmp_path, family)
    try:
        controller.device = NativeDevice(worker)
        controller.initialized = controller.acquiring = True
        with pytest.raises(RuntimeError):
            worker.call("crash")
        controller.closeCommunication()
        assert_lost(case)
        assert controller.device is None and worker._process.poll() is not None
        controller._restore_on_ui_state()
        assert not case.parent.onAction.state
        assert controller.shutdownCommunication() is False
        assert other.call("echo", "independent") == "independent"
        fresh = proxy(binary, tmp_path, family)
        try:
            assert fresh.call("echo", "reconnected") == "reconnected"
            assert controller._dispose_device() is True
        finally:
            fresh.close(grace_s=0)
    finally:
        worker.close(grace_s=0)
        other.close(grace_s=0)


@pytest.mark.parametrize("opening", [True, False])
def test_explicit_native_disconnect_reaps_blocked_process_without_controller_lock(case, binary, tmp_path, opening):
    from test_native_device_recovery import NativeDevice, proxy

    controller = case.controller
    family = "amx_hd" if case.parent.name == "AMX_HD" else "amx"
    worker = proxy(binary, tmp_path, family)
    controller.device = NativeDevice(worker, opening=opening)
    controller.initialized = controller.acquiring = not opening
    failures = []

    def blocked():
        with controller.lock:
            try:
                worker.call("native_hang", rpc_timeout_s=300., _io_timeout_s=30.)
            except Exception as exc:
                failures.append(exc)

    thread = Thread(target=blocked)
    thread.start()
    try:
        deadline = time.monotonic() + 2.
        while worker._active is None and time.monotonic() < deadline:
            time.sleep(.005)
        assert worker._active is not None
        started = time.monotonic()
        controller.closeCommunication()
        thread.join(2.)
        assert time.monotonic() - started < 3.
        assert not thread.is_alive() and failures
        assert worker._process.poll() is not None and controller.device is None
        assert_lost(case)
    finally:
        worker.close(grace_s=0)
        thread.join(2.)
        assert not thread.is_alive()


def test_transport_consumes_local_event_before_serializing_rpc(case, monkeypatch):
    from test_native_worker import TRANSPORT

    start(case)
    proxy = TRANSPORT.NativeWorkerProxy.__new__(TRANSPORT.NativeWorkerProxy)
    proxy.session, proxy.family = "actual-encoder-test", "amx"
    proxy._closed = False
    proxy._process = SimpleNamespace(poll=lambda: None)
    requests = []

    def request(op, timeout_s, *, _cancel_event=None, **fields):
        assert _cancel_event is case.controller._native_cancel_event
        assert fields["kwargs"] == {"timeout_s": .2}
        json.dumps(fields, allow_nan=False)
        requests.append((op, timeout_s, fields))
        return {}

    monkeypatch.setattr(proxy, "_request", request)
    object.__setattr__(case.controller.device, "_backend", proxy)
    case.controller._device_call(case.controller.device, "collect_housekeeping", timeout_s=.2)
    assert requests[0][0] == "call" and requests[0][2]["method"] == "collect_housekeeping"
    assert requests[0][1] > 0.
    case.controller._native_cancel_event.set()
    with pytest.raises(RuntimeError, match="before dispatch"):
        case.controller._device_call(case.controller.device, "collect_housekeeping", timeout_s=.2)
    assert len(requests) == 1


@pytest.mark.parametrize("folder", FOLDERS)
@pytest.mark.parametrize("scenario", ["connect_cancel", "poll_crash"])
def test_native_buttons_and_reconnect_through_real_qt(folder, scenario, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), folder, scenario, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def qt_probe(folder, scenario, output):
    try:
        from PyQt6 import QtCore, QtWidgets
        from PyQt6.QtTest import QTest
    except ImportError:
        return 77
    app = QtWidgets.QApplication([])
    with pytest.MonkeyPatch.context() as patch:
        case = make_case(folder, Path(output), patch, direct_gui=False)
        parent, controller = case.parent, case.controller
        window = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(window)
        status, enabled = QtWidgets.QLabel(), QtWidgets.QLabel()
        buttons = [QtWidgets.QPushButton("ON"), QtWidgets.QPushButton("Device ON")]
        init_button = QtWidgets.QPushButton("Initialize")
        close_button = QtWidgets.QPushButton("Close")
        recording_button = QtWidgets.QPushButton("Record")
        for button in buttons:
            button.setCheckable(True)
            button.setChecked(True)
            layout.addWidget(button)
        layout.addWidget(status)
        layout.addWidget(enabled)
        for button in (init_button, close_button, recording_button):
            layout.addWidget(button)
        parent.liveDisplay = SimpleNamespace(initCommunicationAction=init_button,
                                             closeCommunicationAction=close_button,
                                             recordingAction=recording_button)
        parent.recordingAction = recording_button
        del parent._sync_acquisition_controls

        class Action:
            def __init__(self, button):
                self.button = button

            @property
            def state(self):
                return self.button.isChecked()

            @state.setter
            def state(self, value):
                assert QtCore.QThread.currentThread() == app.thread()
                with QtCore.QSignalBlocker(self.button):
                    self.button.setChecked(value)

        parent.onAction, parent.deviceOnAction = [Action(button) for button in buttons]

        def refresh():
            assert QtCore.QThread.currentThread() == app.thread()
            status.setText(controller.main_state)
            enabled.setText(controller.device_enabled_state)
            parent._sync_acquisition_controls()

        parent._update_status_widgets = refresh
        for button in buttons:
            button.toggled.connect(parent.setOn)
        parent.initializeCommunication = lambda: launch_init()
        workers = []

        def launch_init():
            worker = Thread(target=controller.initializeCommunication)
            workers.append(worker)
            worker.start()

        init_button.clicked.connect(launch_init)

        def settle(condition):
            deadline = time.monotonic() + 3.
            while not condition():
                app.processEvents()
                time.sleep(.005)
                assert time.monotonic() < deadline, "Qt native lifecycle did not settle"
            app.processEvents()

        try:
            window.resize(420, 200)
            window.show()
            app.processEvents()
            proxy = case.proxies[0]
            if scenario == "connect_cancel":
                proxy.blocked_method = "connect"
                launch_init()
                settle(proxy.entered.is_set)
                QTest.mouseClick(buttons[1], QtCore.Qt.MouseButton.LeftButton)
            else:
                controller.initializeCommunication()
                parent._set_on_ui_state(False)
                case.completions.popleft()()
                parent._set_on_ui_state(True)
                controller.acquiring = True
                proxy.failures["collect_housekeeping"] = (TimeoutError("DLL watchdog expired"), True)
                worker = Thread(target=controller.readNumbers)
                workers.append(worker)
                worker.start()
            settle(lambda: all(not worker.is_alive() for worker in workers))
            settle(lambda: all(not button.isChecked() for button in buttons))
            assert status.text() == "Shutdown unconfirmed" and enabled.text() == "Unknown"
            assert controller.device is None and not controller.initialized
            assert not case.resumes and proxy.enabled
            assert init_button.isEnabled() and not close_button.isEnabled()
            assert not recording_button.isEnabled() and not parent.recording
            assert window.grab().save(str(Path(output) / f"{folder}-{scenario}.png"))
            fresh = NativeProxy(proxy.family)
            case.proxies.append(fresh)
            QTest.mouseClick(init_button, QtCore.Qt.MouseButton.LeftButton)
            settle(lambda: len(case.created) == 2 and not controller.initializing)
            assert all(not button.isChecked() for button in buttons)
            assert not init_button.isEnabled() and close_button.isEnabled()
            assert case.controller.device is case.created[1] and case.successes[-1] is True
            assert not any(call[0] in {"set_device_enabled", "load_config"} for call in fresh.calls)
            settle(lambda: bool(case.completions))
            case.completions.popleft()()
            assert not case.resumes and not recording_button.isEnabled()
            QTest.mouseClick(buttons[0], QtCore.Qt.MouseButton.LeftButton)
            assert all(button.isChecked() for button in buttons)
            assert case.resumes == [{"parallel": True}]
        finally:
            controller._native_cancel_event.set()
            for worker in workers:
                worker.join(3.)
                assert not worker.is_alive()
            for facade in case.created:
                facade.close()
            window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(qt_probe(*sys.argv[1:]))
