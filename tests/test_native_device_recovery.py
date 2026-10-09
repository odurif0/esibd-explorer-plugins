"""GUI retirement of actual native processes, with no instrument DLL loaded."""
from types import SimpleNamespace
import threading
import time

import pytest

from test_native_worker import TRANSPORT, ROOT, binary  # noqa: F401
from test_shutdown_confirmation_regressions import controller_for


class NativeDevice:
    NO_ERR = 0
    _backend_mode = "process"
    _open_failed = _failed_open_released = False

    def __init__(self, backend, *, opening=False):
        self._backend = backend
        self._opening_in_progress = opening

    @property
    def connected(self):
        return not self._backend.closed

    @property
    def _transport_poisoned(self):
        return self._backend.closed

    @property
    def _dll_port_claimed(self):
        return self._backend._process.poll() is None

    def disconnect(self, **kwargs):
        raise AssertionError("A dead/hung process must not receive a new hardware command")

    def close(self):
        return self._backend.close(grace_s=0)


def proxy(binary, tmp_path, family):
    worker = TRANSPORT.NativeWorkerProxy(ROOT, "test", {}, log_dir=tmp_path,
                                        command=[str(binary), "--family", "test"], stop_grace_s=.05)
    # Only the GUI device marker is adapted; the child is explicitly the fault
    # backend, not evidence that an instrument DLL or real COM port was tested.
    worker.family = family
    return worker


def gui(family):
    module, controller, messages = controller_for(family)
    parent = controller.controllerParent
    parent.on = True
    parent._set_on_ui_state = lambda value: setattr(parent, "on", bool(value))
    parent.isOn = lambda: parent.on
    controller._sync_status_to_gui = lambda **kwargs: None
    controller.initialized, controller.acquiring = True, True
    controller.initializing = False
    parent.com = 1
    return module, controller, parent, messages


def toolbar(module, controller, parent, family):
    cls = getattr(module, {"ampr": "AMPRDevice", "dmmr": "DMMRDevice", "psu": "PSUDevice"}[family])
    parent.controller = controller
    parent.useOnOffLogic = True
    parent.onAction = SimpleNamespace(state=True)
    parent.isOn = lambda: parent.onAction.state
    parent._set_on_ui_state = lambda value: setattr(parent.onAction, "state", bool(value))
    parent.stopAcquisition = lambda: setattr(controller, "acquiring", False)
    for name in ("_sync_local_on_action", "_sync_toolbar_communication_controls",
                 "_sync_acquisition_controls", "_update_status_widgets", "_stop_refresh_timer", "_sync_channels",
                 "_drop_pending_setpoints"):
        setattr(parent, name, lambda: None)
    parent.print = controller.print
    parent.shutdownCommunication = lambda: cls.shutdownCommunication(parent)
    return cls


@pytest.fixture(scope="module")
def qt_app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.mark.parametrize("family", ["ampr", "dmmr", "psu"])
@pytest.mark.parametrize("callback", ["off", "on", "status", "init", "device_off"])
def test_queued_old_success_off_on_status_cannot_touch_replacement(binary, tmp_path, monkeypatch, qt_app, family, callback):
    module, controller, parent, _ = gui(family)
    cls = toolbar(module, controller, parent, family)
    parent._set_on_ui_state = lambda value: cls._set_on_ui_state(parent, value)
    old = proxy(binary, tmp_path, family)
    fresh = proxy(binary, tmp_path, family)
    try:
        controller.device = NativeDevice(old)
        controller._native_initialization_required = True
        controller._native_initialization_ready = (controller._native_session_token, controller.device)
        controller._sync_status_to_gui = type(controller)._sync_status_to_gui.__get__(controller)
        touched = []
        parent._update_status_widgets = lambda: touched.append("status")
        def queue():
            if callback == "init":
                module._invoke_gui_callback(controller.initComplete)
            elif callback == "device_off":
                parent._set_on_ui_state(False)
            else:
                getattr(controller, {"off": "_restore_off_ui_state", "on": "_restore_on_ui_state",
                                     "status": "_sync_status_to_gui"}[callback])()
        thread = threading.Thread(target=queue)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        controller.closeCommunication()
        monkeypatch.setattr(type(controller).__mro__[1], "initializeCommunication", lambda self: None, raising=False)
        controller.initializeCommunication()
        controller.device = NativeDevice(fresh)
        controller.initialized = False
        replacement_on = callback != "on"
        parent.onAction.state = replacement_on
        touched.clear()  # Current-session retirement updates are legitimate.
        for _ in range(5):
            qt_app.processEvents()
        assert parent.onAction.state is replacement_on
        assert not controller.initialized and not touched
        assert controller.device._backend is fresh
        assert fresh.call("echo", "replacement") == "replacement"
    finally:
        old.close(grace_s=0)
        fresh.close(grace_s=0)


@pytest.mark.parametrize("family", ["ampr", "dmmr", "psu"])
def test_toolbar_dead_worker_off_is_reconnectable(binary, tmp_path, family):
    module, controller, parent, _ = gui(family)
    worker = proxy(binary, tmp_path, family)
    toolbar(module, controller, parent, family)
    try:
        controller.device = NativeDevice(worker)
        with pytest.raises(RuntimeError):
            worker.call("crash")
        parent.shutdownCommunication()
        assert not parent.onAction.state and controller.device is None
        assert not controller.initialized and worker._process.poll() is not None
        assert controller.main_state == "Disconnected: shutdown unconfirmed"
    finally:
        worker.close(grace_s=0)


@pytest.mark.parametrize("family", ["ampr", "dmmr", "psu"])
def test_force_reconnect_validated_init_then_verified_off_not_bare_close(binary, tmp_path, monkeypatch, family):
    module, controller, parent, _ = gui(family)
    old = proxy(binary, tmp_path, family)
    fresh = proxy(binary, tmp_path, family)
    cls = toolbar(module, controller, parent, family)
    calls = []
    class Hardware(NativeDevice):
        hardware_connected = True
        verified = False
        @property
        def connected(self):
            return self.hardware_connected
        @property
        def _dll_port_claimed(self):
            return self.hardware_connected
        def shutdown(self, **kwargs):
            assert self._backend.call("echo", "verified OFF") == "verified OFF"
            calls.append("verified OFF")
            self.verified = True
            self.hardware_connected = False
            return True
        def disconnect(self, **kwargs):
            assert self.verified, "Bare Close skipped verified hardware OFF"
            calls.append("disconnect")
            self.hardware_connected = False
            return True
    try:
        controller.device = NativeDevice(old)
        controller.closeCommunication()
        assert old._process.poll() is not None
        monkeypatch.setattr(type(controller).__mro__[1], "initializeCommunication", lambda self: None, raising=False)
        controller.initializeCommunication()
        controller.device = Hardware(fresh)
        controller._cancel_ramp = False
        parent.onAction.state = False
        controller._native_initialization_ready = (controller._native_session_token, controller.device)
        controller.initComplete()
        assert controller.initialized and controller._forced_close_state is None
        assert controller._native_shutdown_unconfirmed  # Fresh Open cannot prove old outputs were OFF.
        if family == "psu":
            controller._perform_shutdown_sequence_unlocked = lambda **kwargs: controller.device.shutdown() and []
            controller._confirm_shutdown_unlocked = lambda **kwargs: (controller.device.verified, "test readback")
        elif family == "dmmr":
            # The actual child is the fault backend, not a vendor DLL emulator.
            controller._native_call = lambda device, method, **kwargs: getattr(device, method)(**kwargs)
        parent.onAction.state = True
        cls.closeCommunication(parent)
        assert calls[0] == "verified OFF"
        assert not parent.onAction.state and controller.device is None
        assert controller.main_state == "Disconnected" and fresh._process.poll() is not None
    finally:
        old.close(grace_s=0)
        fresh.close(grace_s=0)


@pytest.mark.parametrize("family", ["ampr", "dmmr", "psu"])
def test_healthy_unconfirmed_toolbar_off_keeps_owner_and_retry(binary, tmp_path, family):
    module, controller, parent, _ = gui(family)
    worker = proxy(binary, tmp_path, family)
    toolbar(module, controller, parent, family)
    device = NativeDevice(worker)
    device.shutdown = lambda **kwargs: False
    controller.device = device
    if family == "psu":
        controller._perform_shutdown_sequence_unlocked = lambda **kwargs: []
        controller._confirm_shutdown_unlocked = lambda **kwargs: (False, "unconfirmed readback")
    elif family == "dmmr":
        controller._native_call = lambda device, method, **kwargs: getattr(device, method)(**kwargs)
    try:
        parent.shutdownCommunication()
        assert parent.onAction.state and controller.device is device and controller.initialized
        assert not worker.closed and controller.main_state == "Shutdown unconfirmed"
        assert worker.call("echo", "available for retry") == "available for retry"
    finally:
        worker.close(grace_s=0)


@pytest.mark.parametrize("family", ["ampr", "dmmr", "psu"])
def test_failed_reap_does_not_escape_or_release_owner(family):
    _, controller, _, messages = gui(family)
    def failed_close(**kwargs):
        raise OSError("injected reap failure")
    device = SimpleNamespace(_backend=SimpleNamespace(session="failed", family=family, closed=True,
                                                      call_method=failed_close, close=failed_close))
    controller.device = device
    assert controller._retire_native_worker() is False
    controller._restore_on_ui_state()
    assert controller.device is device
    assert not controller.initialized and not controller.acquiring
    assert controller.main_state == "Shutdown unconfirmed"
    assert any("release failed" in message for message in messages)


@pytest.mark.parametrize("family", ["ampr", "dmmr", "psu"])
def test_late_state_read_failure_cannot_retire_replacement(binary, tmp_path, family):
    _, controller, _, _ = gui(family)
    worker = proxy(binary, tmp_path, family)
    fresh = NativeDevice(worker)
    def late_failure(**kwargs):
        controller.device = fresh
        raise RuntimeError("Native worker pipe closed: obsolete session")
    controller.device = SimpleNamespace(get_state=late_failure, collect_housekeeping=late_failure)
    controller.main_state = "Replacement ready"
    try:
        controller._update_state()
        assert controller.device is fresh and controller.initialized
        assert controller.main_state == "Replacement ready" and controller.errorCount == 0
        assert worker.call("echo", "replacement alive") == "replacement alive"
    finally:
        worker.close(grace_s=0)


@pytest.mark.parametrize("family", ["ampr", "dmmr", "psu"])
def test_crashed_worker_is_off_and_reconnectable_not_confirmed_safe(binary, tmp_path, family):
    _, controller, parent, messages = gui(family)
    worker = proxy(binary, tmp_path, family)
    other = proxy(binary, tmp_path, family)
    try:
        controller.device = NativeDevice(worker)
        with pytest.raises(RuntimeError):
            worker.call("crash")
        controller._handle_transport_loss()
        assert controller.device is None and not controller.initialized and not controller.acquiring
        assert not parent.on and worker._process.poll() is not None
        assert controller.main_state == "Disconnected: shutdown unconfirmed"
        assert any("not confirmed" in message for message in messages)
        assert controller.shutdownCommunication() is False
        assert other.call("echo", "independent") == "independent"
        fresh = proxy(binary, tmp_path, family)
        try:
            controller.device = NativeDevice(fresh)
            controller._restore_on_ui_state()
            assert parent.on and fresh.call("echo", "reconnected") == "reconnected"
        finally:
            fresh.close(grace_s=0)
    finally:
        worker.close(grace_s=0)
        other.close(grace_s=0)


@pytest.mark.parametrize("family", ["ampr", "dmmr", "psu"])
@pytest.mark.parametrize("opening", [True, False])
def test_explicit_disconnect_reaps_hung_worker_without_controller_lock(binary, tmp_path, family, opening):
    _, controller, parent, _ = gui(family)
    worker = proxy(binary, tmp_path, family)
    controller.device = NativeDevice(worker, opening=opening)
    failures = []
    def blocked():
        with controller.lock:
            try:
                worker.call("native_hang", rpc_timeout_s=300., _io_timeout_s=30.)
            except Exception as exc:
                failures.append(exc)
    thread = threading.Thread(target=blocked)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while worker._active is None and time.monotonic() < deadline:
            time.sleep(.005)
        assert worker._active is not None
        started = time.monotonic()
        controller.closeCommunication()
        thread.join(timeout=2)
        assert time.monotonic() - started < 3
        assert not thread.is_alive() and failures
        assert worker._process.poll() is not None and controller.device is None
        assert not controller.initialized and not parent.on
        assert controller.main_state == "Disconnected: shutdown unconfirmed"
    finally:
        worker.close(grace_s=0)
        thread.join(timeout=2)
