"""ESI cancellation, connection generations, GUI boundaries and log destination."""

import sys
import threading
from types import SimpleNamespace

import pytest

from test_esi_driver_behavior import RUNTIME_NAME, _load_runtime
from test_esi_heater_activation import rig  # noqa: F401
from test_esi_plugin_behavior import _load_plugin


@pytest.mark.parametrize("address", [1, 2])
@pytest.mark.parametrize("phase", [
    "before_dispatch", "module_dispatch", "module_write", "module_readback", "global_dispatch",
])
def test_hv_on_cancellation_never_opens_shared_gate(rig, monkeypatch, address, phase):
    cancel = threading.Event()
    dispatch = rig.driver._call_locked_with_timeout
    write = rig.base.set_module_activation_state
    read = rig.base.get_hv_supply_params_pwm

    def call(function, timeout, step, *args, **kwargs):
        if ((phase == "module_dispatch" and step.startswith("set_hv_module_active"))
                or (phase == "global_dispatch" and step == "set_global_active")):
            cancel.set()
        return dispatch(function, timeout, step, *args, **kwargs)

    def set_module(self, module, active):
        result = write(self, module, active)
        if phase == "module_write" and active:
            cancel.set()
        return result

    def get_pwm(self, module):
        result = read(self, module)
        if phase == "module_readback":
            cancel.set()
        return result

    monkeypatch.setattr(rig.driver, "_call_locked_with_timeout", call)
    monkeypatch.setattr(rig.base, "set_module_activation_state", set_module)
    monkeypatch.setattr(rig.base, "get_hv_supply_params_pwm", get_pwm)
    if phase == "before_dispatch":
        cancel.set()
    with pytest.raises(InterruptedError, match="cancel"):
        rig.driver.set_output_active(address, True, timeout_s=.2, cancel_event=cancel)
    assert ("enable", True) not in rig.calls
    if phase in ("before_dispatch", "module_dispatch"):
        assert ("module", address, True) not in rig.calls
    # Cancellation must never prevent a following OFF from deactivating HV.
    assert rig.driver.set_output_active(address, False, timeout_s=.2, cancel_event=cancel) is False
    assert rig.state.active[address] is False


def test_plugin_forwards_hv_cancellation_to_worker():
    module = _load_plugin()
    c = module.ESIController(SimpleNamespace(poll_timeout_s=.2, isOn=lambda: True))
    c.initialized = True
    received = []
    c.device = SimpleNamespace(
        set_hv_module_target=lambda address, target, **kwargs: target,
        set_output_active=lambda address, active, **kwargs: received.append(kwargs["cancel_event"]) or active,
    )
    channel = SimpleNamespace(is_heat_channel=lambda: False, module_address=lambda: 1,
                              enabled=True, value=700., name="HV1")
    c.applyValue(channel)
    assert received == [c._output_cancel]


def test_on_transition_does_not_use_explorers_widget_updating_stop(monkeypatch):
    module = _load_plugin()
    main_thread = threading.current_thread()
    writes, queued, errors = [], [], []

    class Parent:
        connect_timeout_s = .2
        def isOn(self):
            return True
        def getChannels(self):
            return []
        @property
        def recording(self):
            return True
        @recording.setter
        def recording(self, value):
            assert threading.current_thread() is main_thread
            writes.append(value)

    c = module.ESIController(Parent())
    c.initialized = c.acquiring = True
    c.device = SimpleNamespace(set_output_active=lambda *args, **kwargs: False,
                               set_global_active=lambda *args, **kwargs: True)
    monkeypatch.setattr(module, "_invoke_gui_callback", queued.append)
    monkeypatch.setattr(module.DeviceController, "stopAcquisition",
                        lambda self: pytest.fail("Explorer's stop updates widgets on the calling thread"))

    def toggle():
        try:
            c.toggleOn()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=toggle)
    worker.start()
    worker.join(3.)
    assert not worker.is_alive() and not errors
    assert not writes
    for callback in queued:
        callback()
    assert writes == [False] and c.acquiring


@pytest.mark.parametrize("replacement", ["new_backend", "new_request", "new_request_same_backend"])
def test_old_off_callback_cannot_modify_new_connection(monkeypatch, replacement):
    module = _load_plugin()
    action = SimpleNamespace(state=True)
    c = module.ESIController(SimpleNamespace(onAction=action))
    c.device = None if replacement == "new_request" else object()
    queued = []
    monkeypatch.setattr(module, "_invoke_gui_callback", queued.append)
    c._restore_off_ui_state()
    if replacement == "new_backend":
        c.device = object()
    else:
        c._connection_generation += 1
    c.resume_session = True
    queued[0]()
    assert action.state is True and c.resume_session


def test_old_acquisition_stop_cannot_stop_recording_after_reconnect(monkeypatch):
    module = _load_plugin()
    parent = SimpleNamespace(recording=True)
    c = module.ESIController(parent)
    queued = []
    monkeypatch.setattr(module, "_invoke_gui_callback", queued.append)
    c._stop_local_acquisition()
    c._connection_generation += 1
    queued[-1]()
    assert parent.recording is True


@pytest.mark.parametrize("forced", [False, True])
def test_new_on_requested_during_final_cleanup_cannot_receive_old_off(monkeypatch, forced):
    module = _load_plugin()
    parent = SimpleNamespace(onAction=SimpleNamespace(state=False), connect_timeout_s=.2, recording=True)
    c = module.ESIController(parent)
    queued = []
    monkeypatch.setattr(module, "_invoke_gui_callback", queued.append)
    def request_on():
        c._connection_generation += 1
        c.resume_session = True
        parent.onAction.state = True
    def reset(**kwargs):
        if forced and c.device is None:
            request_on()
    c.initializeValues = reset
    c.device = SimpleNamespace(disconnect=lambda **kwargs: True,
                               force_close_transport=lambda **kwargs: True,
                               close=lambda: None if forced else request_on())
    if forced:
        assert c._release_failed_transport(c.device)
    else:
        assert c._shutdown_communication_unlocked()
    for callback in queued:
        callback()
    assert parent.onAction.state is True and c.resume_session


def test_init_complete_does_not_read_configurations_on_gui_thread():
    module = _load_plugin()
    c = module.ESIController(SimpleNamespace(ensureFixedChannels=lambda **kwargs: None, isOn=lambda: False))
    c.device = SimpleNamespace(list_configs=lambda **kwargs: pytest.fail("hardware I/O on the GUI thread"))
    c._refresh_available_configs = lambda: pytest.fail("configuration reads belong in runInitialization")
    c.initComplete()
    assert c.initialized and c.acquiring


def initialization_control(monkeypatch, tmp_path, list_configs, *, resumed=False):
    module = _load_plugin()
    constructed, published = [], []

    class Driver:
        _process_backend_disabled_reason = ""
        def __init__(self, **kwargs):
            constructed.append(kwargs)
        def connect(self, **kwargs):
            pass
        def set_global_active(self, *args, **kwargs):
            pass
        def collect_identity(self, **kwargs):
            return {}
        def force_safe_off(self, **kwargs):
            pass
        def configure_hv_max_voltage_steps(self, *args, **kwargs):
            pass
        def select_hv_voltage_adc(self, *args, **kwargs):
            pass
        def collect_diagnostics(self, **kwargs):
            return {}
        def list_configs(self, **kwargs):
            return list_configs()
        def disconnect(self, **kwargs):
            return True
        def close(self):
            pass

    parent = SimpleNamespace(com=16, baudrate=230400, connect_timeout_s=.2, poll_timeout_s=.2,
                             heat_voltage_limit_v=0., heat_current_limit_a=0., recording=False,
                             pluginManager=SimpleNamespace(Settings=SimpleNamespace(dataPath=tmp_path)),
                             onAction=SimpleNamespace(state=True))
    c = module.ESIController(parent)
    c.initializing = True
    c.resume_session = resumed
    c._apply_snapshot = lambda snapshot: None
    c.signalComm = SimpleNamespace(initCompleteSignal=SimpleNamespace(
        emit=lambda: published.append(c.available_configs_text)))
    monkeypatch.setattr(module, "_get_esi_driver_class", lambda: Driver)
    return c, constructed, published


@pytest.mark.parametrize("resumed", [False, True])
def test_configurations_are_read_before_ready_in_initialization_worker(monkeypatch, tmp_path, resumed):
    threads = []
    def configs():
        threads.append(threading.current_thread().name)
        return [{"index": 1, "name": "Spray"}]
    c, constructed, published = initialization_control(monkeypatch, tmp_path, configs, resumed=resumed)
    worker = threading.Thread(target=c.runInitialization, name="ESI initThread")
    worker.start()
    worker.join(3.)
    assert not worker.is_alive() and not c.initializing
    assert threads == ["ESI initThread"] and published == ["1: Spray"]
    assert constructed[0]["log_dir"] == tmp_path / "logs" / "esi"


@pytest.mark.parametrize("resumed", [False, True])
def test_off_during_config_read_does_not_publish_retired_connection(monkeypatch, tmp_path, resumed):
    entered, finish = threading.Event(), threading.Event()
    def configs():
        entered.set()
        assert finish.wait(3.)
        return [{"index": 1, "name": "Old config"}]
    c, _, published = initialization_control(monkeypatch, tmp_path, configs, resumed=resumed)
    worker = threading.Thread(target=c.runInitialization)
    try:
        worker.start()
        assert entered.wait(3.)
        c.closeCommunication()
    finally:
        finish.set()
        worker.join(3.)
    assert not worker.is_alive() and not published and not c.initializing
    assert c.device is None and c.main_state == "Disconnected"
    assert c.available_configs == []


def test_driver_log_is_written_in_explorer_data_directory(monkeypatch, tmp_path):
    c, constructed, _ = initialization_control(monkeypatch, tmp_path, lambda: [])
    c.runInitialization()
    log_dir = constructed[0]["log_dir"]
    _load_runtime()
    runtime = sys.modules[RUNTIME_NAME + ".esi.esi"]
    base = sys.modules[RUNTIME_NAME + ".esi.esi_base"].ESIBase
    monkeypatch.setattr(base, "__init__", lambda *args, **kwargs: None)
    driver = runtime._ESIController("data_log_test", 16, log_dir=log_dir)
    try:
        driver.logger.info("ESI data directory marker")
        path = log_dir / "esi_data_log_test.log"
        assert "ESI data directory marker" in path.read_text()
        handler, = driver.logger.handlers
        assert handler.maxBytes == 1_000_000 and handler.backupCount == 3
    finally:
        for handler in list(driver.logger.handlers):
            handler.close()
            driver.logger.removeHandler(handler)


def test_cancelled_initial_diagnostics_cannot_publish_old_snapshot(monkeypatch, tmp_path):
    c, _, published = initialization_control(monkeypatch, tmp_path, lambda: [])
    driver_type = c.runInitialization.__globals__["_get_esi_driver_class"]()
    def read(self, **kwargs):
        c.closeCommunication()
        return {"stale": True}
    monkeypatch.setattr(driver_type, "collect_diagnostics", read)
    snapshots = []
    c._apply_snapshot = snapshots.append
    c.runInitialization()
    assert not snapshots and not published
    assert c.device is None and c.main_state == "Disconnected"
