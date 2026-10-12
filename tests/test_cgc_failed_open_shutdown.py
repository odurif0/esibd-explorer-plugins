"""OFF/Explorer close must retire failed Open, not attempt an output shutdown.

The real runtime, native-call workers and registry are used; only DLL functions
are simulated. Successful opening followed by a command timeout is NOT eligible.
"""
import pytest

from support.shutdown import FAMILIES


def close(controller, action):
    if action == "shutdown":
        return controller.shutdownCommunication()
    controller.closeCommunication()
    return None


@pytest.mark.parametrize("action", ["shutdown", "close"])
def test_close_while_failed_open_is_pending_keeps_connection_state(cgc_plugin, cgc_rig, action):
    c, hw = cgc_plugin.controller, cgc_rig.hw
    hw.open_release.clear()
    c.initializeCommunication()
    old = c.device
    assert c.main_state == "Connection pending"
    for _ in range(2):
        result = close(c, action)
        if action == "shutdown":
            assert result is False
        assert c.device is old and old._dll_port_claimed
        assert c.main_state == "Connection pending"
        assert not c.initialized and not cgc_plugin.parent.onAction.state
        assert [name for name, _ in hw.operations] == ["open"]
    assert not any("shutdown could not be confirmed" in str(m).lower()
                   for m in cgc_plugin.messages)


@pytest.mark.parametrize("action", ["shutdown", "close"])
@pytest.mark.parametrize("late_status", [0, -2])
def test_close_after_failed_open_returns_releases_without_output_commands(cgc_plugin, cgc_rig, action, late_status):
    c, hw = cgc_plugin.controller, cgc_rig.hw
    hw.open_status = late_status
    hw.open_release.clear()
    c.initializeCommunication()
    old = c.device
    hw.finish_open()
    assert hw.count("close") == 0
    result = close(c, action)
    if action == "shutdown":
        assert result is True
    assert c.device is None and not old._dll_port_claimed
    assert c.main_state == "Disconnected"
    assert not c.initialized and not cgc_plugin.parent.onAction.state
    assert [name for name, owner in hw.operations if owner == id(old)] == [
        "open", "close", "dispose"]
    assert old._transport_poisoned and old.thread_lock.locked()
    close(c, action)
    assert hw.count("close") == 1 and hw.count("open") == 1


@pytest.mark.parametrize("action", ["shutdown", "close"])
def test_failed_open_close_error_is_not_a_success_or_output_shutdown(cgc_plugin, cgc_rig, action):
    c, hw = cgc_plugin.controller, cgc_rig.hw
    hw.open_status, hw.close_status = -2, -3
    c.initializeCommunication()
    old = c.device
    result = close(c, action)
    if action == "shutdown":
        assert result is False
    assert c.device is old and old._dll_port_claimed
    assert c.main_state == "Connection pending"
    assert not c.initialized and not cgc_plugin.parent.onAction.state
    assert set(name for name, _ in hw.operations) == {"open", "close"}
    assert hw.count("dispose") == 0


@pytest.mark.parametrize("action", ["shutdown", "close"])
@pytest.mark.parametrize("late_status", [0, -3])
def test_failed_open_close_timeout_is_observed_without_replaying_close(cgc_plugin, cgc_rig, action, late_status):
    c, hw = cgc_plugin.controller, cgc_rig.hw
    hw.open_release.clear()
    c.initializeCommunication()
    old = c.device
    hw.finish_open()
    hw.close_status = late_status
    hw.close_release.clear()
    try:
        for _ in range(2):
            result = close(c, action)
            if action == "shutdown":
                assert result is False
            assert c.device is old and old._dll_port_claimed
            assert c.main_state == "Connection pending"
            assert not c.initialized and not cgc_plugin.parent.onAction.state
            assert hw.count("close") == 1 and hw.count("dispose") == 0
        hw.close_release.set()
        hw.join_workers()
        for _ in range(2):
            result = close(c, action)
            if action == "shutdown":
                assert result is (late_status == 0)
            assert hw.count("close") == 1
            if late_status == 0:
                assert c.device is None and not old._dll_port_claimed
                assert c.main_state == "Disconnected"
                assert hw.count("dispose") == 1
            else:
                assert c.device is old and old._dll_port_claimed
                assert c.main_state == "Connection pending"
                assert hw.count("dispose") == 0
            assert not c.initialized and not cgc_plugin.parent.onAction.state
    finally:
        hw.close_release.set()
        hw.join_workers()


@pytest.mark.parametrize("action", ["shutdown", "close"])
def test_close_before_successful_open_returns_cancels_initialization(cgc_plugin, cgc_rig, monkeypatch, action):
    import threading

    c, hw = cgc_plugin.controller, cgc_rig.hw
    cgc_plugin.parent.connect_timeout_s = 2.0
    hw.open_release.clear()
    worker = threading.Thread(target=c.initializeCommunication, daemon=True)
    verified_shutdowns = []
    try:
        worker.start()
        assert hw.open_entered.wait(1)
        old = c.device
        close(c, action)
        assert c.main_state == "Connection pending"
        assert not c.initialized and not cgc_plugin.parent.onAction.state
        assert hw.count("close") == 0 and c.device is old

        # Once Open really succeeds, use the normal verified shutdown path,
        # not raw failed-Open cleanup. Its hardware checks are tested elsewhere.
        def verified_shutdown():
            assert old.connected and not old._open_failed
            assert not old._opening_in_progress
            verified_shutdowns.append(old)
            hw.invoke("verified_shutdown", old)
            assert cgc_rig.disconnect(old) is True
            old.close()
            c.device = None
            c.initialized = False
            c.main_state = "Disconnected"
            cgc_plugin.parent.onAction.state = False
            return True

        monkeypatch.setattr(c, "shutdownCommunication", verified_shutdown)
        hw.finish_open()
        worker.join(3)
        assert not worker.is_alive()
        assert verified_shutdowns == [old]
        assert not cgc_plugin.successes, "a cancelled connection must not publish initComplete"
        assert c.device is None and c.main_state == "Disconnected"
        assert not old._dll_port_claimed and not c.initializing
        assert hw.count("open") == hw.count("close") == 1

        # A later explicit ON is a new request, not the cancelled connection.
        cgc_plugin.parent.onAction.state = True
        c.initializeCommunication()
        assert cgc_plugin.successes == [True]
        assert c.device is not old and len(cgc_plugin.created) == 2
        assert len(verified_shutdowns) == 1
        assert cgc_rig.disconnect(c.device) is True
    finally:
        hw.open_release.set()
        worker.join(4)
        hw.join_workers()


def test_explicit_reinitialization_closes_old_acquisition_before_arming_request(cgc_plugin, cgc_rig, monkeypatch):
    from types import SimpleNamespace

    c = cgc_plugin.controller
    alive, calls, cancelled_at_launch = [True], [], []
    c.acquisitionThread = SimpleNamespace(is_alive=lambda: alive[0])

    def old_close():
        calls.append("old_close")
        alive[0] = False
        if cgc_rig.family == "psu":
            c._output_cancel.set()
        else:
            c._initial_open_close_requested = True

    def host_launch(self):
        # Explorer closes a running acquisition before scheduling initThread.
        if self.acquisitionThread.is_alive():
            self.closeCommunication()
        calls.append("new_request")
        cancelled_at_launch.append(
            self._output_cancel.is_set() if cgc_rig.family == "psu"
            else bool(getattr(self, "_initial_open_close_requested", False))
        )

    monkeypatch.setattr(c, "closeCommunication", old_close)
    monkeypatch.setattr(cgc_plugin.module.DeviceController, "initializeCommunication", host_launch)
    c.initializeCommunication()
    assert calls == ["old_close", "new_request"]
    assert cancelled_at_launch == [False]


@pytest.mark.parametrize("action", ["shutdown", "close"])
def test_close_before_initialization_worker_starts_is_not_forgotten(cgc_plugin, cgc_rig, monkeypatch, action):
    c, hw = cgc_plugin.controller, cgc_rig.hw
    queued = []

    def schedule(self):
        if self.initializing:
            return
        self.initializing = True
        queued.append(self.runInitialization)

    monkeypatch.setattr(cgc_plugin.module.DeviceController, "initializeCommunication", schedule)
    c.initializeCommunication()
    assert c.initializing and len(queued) == 1
    close(c, action)
    # Repeated ON while the previous worker is scheduled is not a new request.
    c.initializeCommunication()
    assert len(queued) == 1
    queued.pop()()
    assert not cgc_plugin.successes and not hw.operations
    assert c.device is None and not c.initialized and not c.initializing

    # Only an explicit ON after the cancelled worker ends starts a new Open.
    cgc_plugin.parent.onAction.state = True
    c.initializeCommunication()
    assert len(queued) == 1
    queued.pop()()
    assert cgc_plugin.successes == [True] and hw.count("open") == 1
    assert cgc_rig.disconnect(c.device)


@pytest.mark.parametrize("action", ["shutdown", "close"])
def test_close_while_constructing_backend_does_not_open_it(cgc_plugin, cgc_rig, monkeypatch, action):
    import threading

    c, hw = cgc_plugin.controller, cgc_rig.hw
    getter_name = FAMILIES[cgc_rig.family][2]
    factory = getattr(cgc_plugin.module, getter_name)()
    entered, release = threading.Event(), threading.Event()

    def slow_factory(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return factory(*args, **kwargs)

    monkeypatch.setattr(cgc_plugin.module, getter_name, lambda: slow_factory)
    failures = []

    def initialize():
        try:
            c.initializeCommunication()
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=initialize)
    try:
        worker.start()
        assert entered.wait(1)
        assert c.initializing and c.device is None
        close(c, action)
        assert hw.count("open") == 0
        release.set()
        worker.join(2)
        assert not worker.is_alive() and not failures
        assert hw.count("open") == 0, hw.operations
        assert not cgc_plugin.successes
        assert c.device is None and not c.initialized
        assert not c.initializing
    finally:
        release.set()
        worker.join(2)


@pytest.mark.parametrize("action", ["shutdown", "close"])
@pytest.mark.parametrize("operation", ["set_enable", "read_voltage"])
def test_shutdown_after_command_timeout_cannot_use_failed_open_cleanup(cgc_plugin, cgc_rig, action, operation):
    import threading

    c, hw = cgc_plugin.controller, cgc_rig.hw
    old = cgc_rig.new()
    assert old.connect(timeout_s=.2)
    old.close = lambda: hw.invoke("dispose", old)
    c.device, c.initialized, c.main_state = old, True, "ST_ON"
    entered, release = threading.Event(), threading.Event()
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            old._call_locked_with_timeout(
                lambda: hw.invoke(operation, old, entered, release), .05, operation)
        assert entered.is_set()
        for finished in (False, True):
            if finished:
                release.set()
                hw.join_workers()
            result = close(c, action)
            if action == "shutdown":
                assert result is False
            assert c.device is old and old._dll_port_claimed
            assert c.initialized and cgc_plugin.parent.onAction.state
            assert c.main_state == "Shutdown unconfirmed"
            assert hw.count("close") == hw.count("dispose") == 0
            assert hw.count("open") == 1
    finally:
        release.set()
        hw.join_workers()
