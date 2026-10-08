"""Resume after an Explorer crash: devices left ON reconnect without any command and adopt the hardware.

The user's rule (2026-10-08): a crash must not interrupt an experiment. At the next start, the
plugins of devices that were ON reconnect, adopt the hardware state unchanged and restart recording.
A normal OFF or Explorer close is never resumed; a normal start keeps everything OFF.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sys
import time
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEVICE_ENTRYPOINTS = sorted(
    path for path in ROOT.glob("*/*_plugin.py")
    if path.parent.name not in {"mscan", "transmission"}
)
START, END = "# ---- crash resume: identical", "# ---- end crash resume ----"


def _block(path):
    text = path.read_text(encoding="utf-8")
    return text[text.index(START):text.index(END) + len(END)]


def test_the_resume_block_is_identical_in_every_device_plugin():
    assert len(DEVICE_ENTRYPOINTS) == 13
    blocks = {path.parent.name: _block(path) for path in DEVICE_ENTRYPOINTS}
    reference = blocks["psu_a"]
    assert all(block == reference for block in blocks.values()), [n for n, b in blocks.items() if b != reference]
    for path in DEVICE_ENTRYPOINTS:
        source = path.read_text(encoding="utf-8")
        for hook in ("_session_start(self)", "_session_request(self", "_session_clear(self)", "_session_sync(self)"):
            assert hook in source, (path.parent.name, hook)


# ---------------------------------------------------------------- the record (block executed alone)

@pytest.fixture
def block(monkeypatch):
    monkeypatch.delattr(sys, "_esibd_explorer_session_token", raising=False)
    namespace = dict(sys=sys, time=time, Path=Path, Any=object,
                     PRINT=types.SimpleNamespace(WARNING="WARNING"), __name__="crash_resume_block")
    exec(compile(_block(ROOT / "psu_a" / "psu_plugin.py"), "crash_resume_block", "exec"), namespace)
    return types.SimpleNamespace(**{k: v for k, v in namespace.items() if k.startswith("_session")})


class FakeDevice:
    def __init__(self, folder, com=16):
        self.name, self.com, self.recording = "PSU_A", com, False
        self.pluginManager = types.SimpleNamespace(Settings=types.SimpleNamespace(configPath=folder))
        self.onAction = types.SimpleNamespace(state=False)
        self.controller = types.SimpleNamespace(resume_session=False, initialized=True)
        self.printed, self.requests = [], []

    def isOn(self):
        return self.onAction.state

    def setOn(self, on=None):
        self.requests.append(on)
        self.onAction.state = bool(on)  # The plugins switch their ON button at once.

    def print(self, text, **kwargs):
        self.printed.append((text, kwargs.get("flag")))

    def toggleRecording(self, on=None, manual=True):
        self.recording = bool(on)


def _record(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def test_the_record_follows_operator_on_and_off(block, tmp_path):
    device = FakeDevice(tmp_path)
    path = tmp_path / "PSU_A.session.json"
    block._session_sync(device)
    assert not path.exists()  # Before the resume decision, nothing is touched.
    block._session_start(device)
    block._session_request(device, True)
    assert not path.exists()  # Requested, not yet ON.
    device.onAction.state = True
    block._session_sync(device)
    record = _record(path)
    assert record["com"] == "16" and record["device"] == "PSU_A" and record["recording"] is False
    assert record["token"] == block._session_token()
    device.recording = True
    block._session_sync(device)
    assert _record(path)["recording"] is True
    # OFF requested, shutdown unconfirmed: the UI comes back ON, but an OFF is never resumed.
    block._session_request(device, False)
    block._session_sync(device)
    assert device.isOn() and not path.exists()
    block._session_request(device, True)
    assert path.exists()
    block._session_clear(device)  # Disconnection or Explorer closing.
    block._session_sync(device)
    assert not path.exists()


def test_only_a_record_left_by_another_explorer_on_the_same_port_is_resumed(block, tmp_path):
    device = FakeDevice(tmp_path)
    path = tmp_path / "PSU_A.session.json"
    path.write_text(json.dumps(dict(token="crashed", com="16", recording=True, time=1.0)), encoding="utf-8")
    assert block._session_left_on(device)["token"] == "crashed"
    assert block._session_left_on(FakeDevice(tmp_path, com=17)) is None
    path.write_text(json.dumps(dict(token=block._session_token(), com="16")), encoding="utf-8")
    assert block._session_left_on(device) is None  # This process wrote it.
    path.write_text("{not json", encoding="utf-8")
    assert block._session_left_on(device) is None
    assert block._session_left_on(types.SimpleNamespace(name="X")) is None  # No Explorer settings.


def test_a_crash_record_schedules_a_resume_that_reconnects_and_restarts_recording(block, tmp_path, monkeypatch):
    scheduled = []
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.QTimer = types.SimpleNamespace(singleShot=lambda ms, callback: scheduled.append((ms, callback)))
    monkeypatch.setitem(sys.modules, "PyQt6", types.ModuleType("PyQt6"))
    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", qtcore)
    device = FakeDevice(tmp_path)
    (tmp_path / "PSU_A.session.json").write_text(
        json.dumps(dict(token="crashed", com="16", recording=True, time=time.time())), encoding="utf-8")
    block._session_start(device)
    assert [ms for ms, _ in scheduled] == [1500] and device._session_ready
    scheduled.pop()[1]()  # The resume itself.
    assert device.requests == [True] and device.controller.resume_session is True
    assert device.printed[0][1] == "WARNING" and "adopted unchanged" in device.printed[0][0]
    scheduled.pop()[1]()  # Recording waits for the resume to finish.
    assert not device.recording and len(scheduled) == 1
    device.controller.resume_session = False
    scheduled.pop()[1]()
    assert device.recording and not scheduled
    # A controller without crash-resume support is reconnected with a normal ON.
    plain = FakeDevice(tmp_path)
    plain.controller = types.SimpleNamespace(initialized=True)
    block._session_resume(plain, dict(time=0, recording=False))
    assert plain.requests == [True] and not hasattr(plain.controller, "resume_session")


# ---------------------------------------------------------------- per plugin: nothing commanded

def test_esi_resume_reads_only_and_adopts_targets_and_active_outputs(monkeypatch):
    from test_esi_plugin_behavior import _ExplorerLikeChannel, _counting_device, _load_plugin

    module = _load_plugin()
    calls = []

    class FakeDriver:
        _process_backend_disabled_reason = ""

        def __init__(self, **kwargs):
            pass

        def __getattr__(self, name):  # Any command not listed below would be recorded and fail the test.
            return lambda *args, **kwargs: calls.append((name, args))

        def connect(self, timeout_s):
            calls.append(("connect",))

        def collect_identity(self, timeout_s):
            calls.append(("identity",))
            return {}

        def collect_diagnostics(self, timeout_s):
            calls.append(("diagnostics",))
            return {"running": True}

    channels = []
    parent = _counting_device(module, channels)
    for key, value in dict(com=16, baudrate=230400, connect_timeout_s=5.0, poll_timeout_s=2.0,
                           heat_voltage_limit_v=10.0, heat_current_limit_a=1.0, heat_power_limit_w=5.0).items():
        setattr(parent, key, value)
    hv1 = _ExplorerLikeChannel(parent, 1, enabled=False, value=0.0)
    hv2 = _ExplorerLikeChannel(parent, 2, enabled=False, value=500.0)
    heat = _ExplorerLikeChannel(parent, 0, enabled=False, value=20.0, heat=True)
    channels.extend([hv1, hv2, heat])
    controller = module.ESIController(parent)
    emitted = []
    controller.signalComm = types.SimpleNamespace(initCompleteSignal=types.SimpleNamespace(emit=lambda: emitted.append(1)))
    controller.initializing = True
    controller.resume_session = True
    monkeypatch.setattr(module, "_get_esi_driver_class", lambda: FakeDriver)

    controller.runInitialization()

    assert calls == [("connect",), ("identity",), ("diagnostics",)] and emitted == [1]

    def apply_snapshot(snapshot):
        assert snapshot == {"running": True}
        controller.targets, controller.module_active = {1: 1000.0, 2: 0.0}, {1: True, 2: False}
        controller.global_enabled = True
        controller.heat_activation, controller.heat_target_temperature_c = {"active": False}, 90.0
    controller._apply_snapshot = apply_snapshot
    controller.initializeValues = lambda reset=False: None
    controller._refresh_available_configs = lambda: None
    controller.print = lambda text, **kwargs: calls.append(("print", text))
    toggles = []
    controller.toggleOnFromThread = lambda parallel=True: toggles.append(parallel)
    parent.ensureFixedChannels = lambda **kwargs: None
    parent.isOn = lambda: True

    controller.initComplete()

    assert (hv1.enabled, hv1.value) == (True, 1000.0) and (hv2.enabled, hv2.value) == (False, 0.0)
    assert (heat.enabled, heat.value) == (False, 90.0)
    assert all(loading for event in hv1.events + heat.events for loading in event[2:])  # No event fired.
    assert toggles == [] and controller.resume_session is False and controller.acquiring is True
    assert any("adopted as it runs" in text for kind, *rest in calls if kind == "print" for text in rest)


def test_esi_on_during_a_resume_keeps_the_running_outputs_selected():
    from test_esi_plugin_behavior import _ExplorerLikeChannel, _counting_device, _load_plugin

    module = _load_plugin()
    channels = []
    device = _counting_device(module, channels)
    hv1 = _ExplorerLikeChannel(device, 1, enabled=True, value=1000.0)
    channels.append(hv1)
    toggles = []
    device.controller = types.SimpleNamespace(initialized=False, initializing=False, transitioning=False,
                                              resume_session=True, toggleOnFromThread=lambda parallel=True: None)
    device.initializeCommunication = lambda: toggles.append("connect")
    device.onAction = types.SimpleNamespace(state=False)
    device._sync_local_on_action = lambda: None
    device.isOn = lambda: device.onAction.state

    device.setOn(True)

    assert hv1.enabled is True and toggles == ["connect"]


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, name, result=None):
        def record(*args, **kwargs):
            self.calls.append(name)
            return result
        return record


def test_ampr_resume_adopts_the_setpoints_it_holds_without_ramp_or_apply():
    from test_ampr_plugin_channel_sync import _load_module

    module = _load_module()
    record = _Recorder()

    class Channel:
        def __init__(self, module_number, number, enabled, value):
            self.module, self.number, self.enabled, self.value, self.real = module_number, number, enabled, value, True
            self.name, self.lastAppliedValue = f"AMPR M{module_number} CH{number}", float("nan")

        def module_address(self):
            return self.module

        def channel_number(self):
            return self.number

        def getParameterByName(self, name):
            return None

    channels = [Channel(2, 1, False, 50.0), Channel(2, 2, True, 20.0)]
    parent = types.SimpleNamespace(loading=False, getChannels=lambda: channels, isOn=lambda: True,
                                   updateValues=record("updateValues"), com=5,
                                   _sync_channels_from_detected_modules=record("sync"))
    controller = module.AMPRController.__new__(module.AMPRController)
    controller.controllerParent = parent
    controller.device, controller.detected_module_ids, controller.detected_modules_text = object(), [], "2"
    controller.main_state, controller.resume_session, controller._cancel_ramp = "ST_ON", True, False
    controller._initial_open_close_requested = False
    controller.initializeValues = lambda reset=False: None
    controller._sync_status_to_gui = lambda **kwargs: None
    controller.startAcquisition = record("startAcquisition")
    controller.toggleOnFromThread = record("toggle")
    controller._begin_transition = record("transition", True)
    controller.print = lambda *args, **kwargs: record.calls.append(("print", args[0]))

    controller.initComplete()

    assert "updateValues" not in record.calls and "toggle" not in record.calls and "transition" not in record.calls
    assert controller.resume_session is True  # Waiting for the first poll's setpoints.
    controller._last_output_targets = {(2, 1): 30.0, (2, 2): 0.0}
    controller._adopt_hardware_setpoints()
    assert [(c.enabled, c.value, c.lastAppliedValue) for c in channels] == [(True, 30.0, 30.0), (True, 0.0, 0.0)]
    assert controller.resume_session is False and parent.loading is False
    assert any(isinstance(c, tuple) and "nothing was ramped" in c[1] for c in record.calls)


@pytest.mark.parametrize("loader", ["amx", "amx_hd"])
def test_amx_resume_loads_no_config_and_changes_no_setting(loader, monkeypatch):
    if loader == "amx":
        from test_amx_plugin_behavior import _load_module
        module = _load_module()
        cls = module.AMXController
    else:
        from test_amx_hd_plugin_behavior import _load_hd_plugin_module
        module = _load_hd_plugin_module()
        cls = module.AMXHDController
    record = _Recorder()
    parent = types.SimpleNamespace(isOn=lambda: True, name=loader.upper(),
                                   _set_config_setting_value=record("set_config"))
    controller = cls.__new__(cls)
    controller.controllerParent, controller.resume_session, controller.errorCount = parent, True, 0
    controller.transitioning, controller.main_state = False, "STATE_STANDBY"

    class RunNow:  # The resume worker, run synchronously.
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(module, "Thread", RunNow)
    snapshot = {"main_state": {"name": "STATE_ON"}, "device_enabled": True}
    controller._controller_lock_section = lambda *args, **kwargs: contextlib.nullcontext()
    controller._collect_startup_snapshot = record("snapshot", snapshot)
    controller._apply_snapshot = record("apply")
    controller._refresh_loaded_config_status = record("loaded_config")
    controller._sync_loaded_config_to_gui = record("gui")
    controller._restart_acquisition_after_transition = record("acquisition")
    controller._sync_status_to_gui = record("status")
    controller._begin_transition = record("transition", True)
    controller.toggleOnFromThread = record("toggle")
    controller._selected_operating_config_index = lambda: -1
    controller._resolved_safety_config = lambda name: 0
    controller.print = lambda *args, **kwargs: record.calls.append(("print", args[0]))

    controller._resume_pending_on_request_after_transport_ready()

    names = [c for c in record.calls if isinstance(c, str)]
    assert names == ["snapshot", "apply", "loaded_config", "gui", "acquisition", "status"], record.calls
    assert controller.resume_session is False
    assert any(isinstance(c, tuple) and "no config was loaded" in c[1] for c in record.calls)


def test_psu_resume_only_connects_and_dmmr_resume_is_a_normal_on():
    from test_psu_plugin_behavior import _load_module as load_psu

    module = load_psu()
    controller = module.PSUController.__new__(module.PSUController)
    printed = []
    controller.controllerParent = types.SimpleNamespace(_sync_channels=lambda: None)
    controller._output_cancel = types.SimpleNamespace(is_set=lambda: False)
    controller.device, controller.resume_session = object(), True
    controller.initializeValues = lambda reset=False: None
    controller._set_loaded_config_text = lambda text: None
    controller._sync_status_to_gui = lambda **kwargs: None
    controller.print = lambda text, **kwargs: printed.append(text)
    controller.toggleOnFromThread = lambda *args, **kwargs: pytest.fail("a PSU resume must not run its ON sequence")

    controller.initComplete()

    assert controller.resume_session is False and "nothing was switched" in printed[0]

    from test_dmmr_plugin_behavior import _load_module as load_dmmr

    module = load_dmmr()
    controller = module.DMMRController.__new__(module.DMMRController)
    controller._initial_open_close_requested, controller.resume_session = False, True
    controller.device, controller.detected_module_ids = None, []
    controller.initializeValues = lambda reset=False: None
    controller._sync_status_to_gui = lambda **kwargs: None
    controller.print = lambda *args, **kwargs: None
    controller.controllerParent = types.SimpleNamespace(isOn=lambda: True)
    controller.initComplete()
    assert controller.resume_session is False  # The DMMR's normal ON follows (measurement only).
