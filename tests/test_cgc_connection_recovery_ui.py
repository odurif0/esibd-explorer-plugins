"""Real Qt ON/close actions must keep failed connection cleanup separate from OFF."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

SLUGS = ("ampr_a", "amx_a", "amx_hd", "psu_a", "esi")


@pytest.mark.parametrize("slug", SLUGS)
@pytest.mark.parametrize("scale", ["1", "1.5"])
@pytest.mark.parametrize("finish", ["retry", "close", "shutdown", "cancel", "queued"])
def test_explicit_qt_connection_actions_keep_failed_open_cleanup_separate(slug, scale, finish, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), slug, str(tmp_path), finish],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt/Explorer sources unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(slug, output, finish="retry"):
    from importlib.metadata import PackageNotFoundError
    import threading
    import time
    from types import MethodType, SimpleNamespace

    try:
        from PyQt6.QtCore import QThread, Qt
        from PyQt6.QtGui import QAction, QIcon
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QLabel, QToolBar, QVBoxLayout, QWidget
        from test_on_off_action_ui import explorer_state_action
        StateAction = explorer_state_action()
    except (ImportError, PackageNotFoundError):
        return 77

    from test_cgc_initial_connection_recovery import SPECS, rig as rig_fixture
    from test_cgc_plugin_connection_recovery import build_plugin

    app = QApplication([])
    with pytest.MonkeyPatch.context() as mp:
        spec = next(spec for spec in SPECS if spec[0] == slug)
        fixture = rig_fixture.__wrapped__(SimpleNamespace(param=spec), mp)
        rig = next(fixture)
        try:
            model = build_plugin(rig, mp)
            module, controller = model.module, model.controller
            controller.initializing = False
            cls = getattr(module, type(controller).__name__.replace("Controller", "Device"))

            # Run Explorer's actual close chain (including super()), not a no-op
            # stand-in: ESI explicitly stops, then the host stops the controller.
            import ast
            from importlib.metadata import distribution
            from textwrap import indent
            source_dir = Path(os.environ.get("ESIBD_EXPLORER_SOURCE", "")) / "esibd"
            if not (source_dir / "plugins.py").is_file():
                source_dir = Path(distribution("esibd-explorer").locate_file("esibd"))
            tree = ast.parse((source_dir / "plugins.py").read_text(encoding="utf-8"))
            ns = {"_StubDevice": module.Device}
            source = "from __future__ import annotations\n"
            for name, base in (("ChannelManager", "_StubDevice"), ("Device", "ChannelManager")):
                host_class = next(node for node in tree.body
                                  if isinstance(node, ast.ClassDef) and node.name == name)
                method = next(node for node in host_class.body
                              if isinstance(node, ast.FunctionDef) and node.name == "closeCommunication")
                source += f"class {name}({base}):\n{indent(ast.unparse(method), '    ')}\n"
            exec(compile(source, str(source_dir / "plugins.py"), "exec"), ns)
            cls.__bases__ = (ns["Device"],)

            class Parent(cls, QWidget):
                def __init__(self):
                    QWidget.__init__(self)

                @property
                def initialized(self):
                    return self.controller.initialized

                @property
                def main_state(self):
                    return self._main_state

                @main_state.setter
                def main_state(self, value):
                    assert QThread.currentThread() == app.thread()
                    self._main_state = value
                    self.status.setText(value)

            parent = Parent()
            parent.status = QLabel(parent)
            parent.controller = controller
            for name, value in vars(model.parent).items():
                if name != "initialized":
                    setattr(parent, name, value)
            controller.controllerParent = parent
            parent.channels = []
            parent.getChannels = lambda: parent.channels
            parent._setting = lambda _name: None
            parent.loading = False
            parent.useOnOffLogic = True
            parent.print = lambda *a, **kw: None
            parent.updateValues = lambda **kw: None
            parent.stopAcquisition = lambda: None
            parent.FREQUENCY_KHZ = getattr(cls, "FREQUENCY_KHZ", "")
            for name in ("_sync_toolbar_communication_controls", "_sync_acquisition_controls",
                         "_update_config_controls", "_update_manual_mode_action",
                         "_update_manual_panel", "_refresh_device_controls", "_update_status_widgets"):
                setattr(parent, name, lambda *a, **kw: None)
            for method in ("_sync_local_on_action", "_set_on_ui_state", "setOn", "_finish_setpoint_edits",
                           "closeCommunication", "shutdownCommunication"):
                if hasattr(cls, method):
                    setattr(parent, method, MethodType(getattr(cls, method), parent))
            sync_name = "_sync_status" if rig.family == "esi" else "_sync_status_to_gui"
            setattr(controller, sync_name, MethodType(getattr(type(controller), sync_name), controller))
            layout = QVBoxLayout(parent)
            parent.titleBar = QToolBar(parent)
            layout.addWidget(parent.titleBar)
            layout.addWidget(parent.status)
            path = Path(module.__file__).parent
            icons = [QIcon(str(path / f"switch-medium_{state}.png")) for state in ("on", "off")]
            kwargs = dict(parentPlugin=parent, iconFalse=icons[0], iconTrue=icons[1], restore=False)
            parent.onAction = StateAction(**kwargs, toolTipFalse="ON", toolTipTrue="OFF")
            parent.deviceOnAction = StateAction(
                **kwargs, toolTipFalse="Turn ON", toolTipTrue="Turn OFF",
                event=lambda checked: parent.setOn(checked),
            )
            parent.isOn = lambda: parent.onAction.state
            parent._set_on_ui_state(False)
            parent.main_state = "Disconnected"
            threads, failures, queued = [], [], []
            hold_worker = finish == "queued"

            def launch(*a, **kw):
                if controller.initializing:
                    return
                assert not any(worker.is_alive() for worker in threads)
                controller.initializing = True

                def run():
                    try:
                        controller.runInitialization()
                    except BaseException as exc:
                        failures.append(exc)
                worker = threading.Thread(target=run, daemon=True)
                threads.append(worker)
                if hold_worker:
                    queued.append(worker)
                else:
                    worker.start()

            # Exercise the real controller's ON-request hook; replace only the
            # base launcher's scheduling, not its per-request cancellation logic.
            mp.setattr(module.DeviceController, "initializeCommunication", launch, raising=False)
            parent.initializeCommunication = controller.initializeCommunication
            button = parent.titleBar.widgetForAction(parent.deviceOnAction)
            close_action = QAction("Close", parent)
            parent.titleBar.addAction(close_action)
            close_button = parent.titleBar.widgetForAction(close_action)
            stop = (getattr(parent, "shutdownCommunication", controller.shutdownCommunication)
                    if finish == "shutdown" else parent.closeCommunication)
            close_action.triggered.connect(lambda checked: stop())
            parent.resize(430, 100)
            parent.show()

            def settle():
                deadline = time.monotonic() + 5
                while any(worker.is_alive() for worker in threads) and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(.002)
                assert not any(worker.is_alive() for worker in threads)
                for _ in range(20):
                    app.processEvents()
                assert not failures, failures

            def pending(*, initializing=False):
                assert controller.main_state == parent.status.text() == "Connection pending", (
                    controller.main_state, parent.status.text(), model.messages, rig.hw.operations, len(threads))
                assert not controller.initialized and controller.initializing is initializing
                assert not parent.onAction.state and not parent.deviceOnAction.state
                assert not button.isChecked() and button.toolTip() == "Turn ON"
                assert button.icon().cacheKey() == icons[0].cacheKey()
                assert not model.successes

            rig.hw.open_release.clear()
            if finish == "queued":
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                assert controller.initializing and len(queued) == 1
                QTest.mouseClick(close_button, Qt.MouseButton.LeftButton)
                # An ON while the cancelled worker is still pending is ignored.
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                assert len(queued) == 1 and not rig.hw.operations
                queued.pop().start()
                settle()
                assert not rig.hw.operations and not model.successes
                assert controller.device is None and not controller.initialized
                assert not controller.initializing
                assert not parent.onAction.state and not parent.deviceOnAction.state
                assert not button.isChecked() and button.toolTip() == "Turn ON"
                parent.grab().save(str(output / f"{slug}-queued-cancelled.png"))
                hold_worker = False
                rig.hw.open_release.set()
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                settle()
                assert model.successes == [True] and len(model.created) == 1
                assert rig.disconnect(controller.device)
                parent.close()
                return 0
            if finish == "cancel":
                parent.connect_timeout_s = 2.0
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                assert rig.hw.open_entered.wait(1)
                old = controller.device
                # Simulate confirmed hardware disable, but retain the real
                # controller, runtime disconnect, Close worker and GUI updates.
                if rig.family == "psu":
                    # PSU performs OFF through individual writes and readbacks,
                    # not the runtime's shutdown method. Simulate those replies.
                    state = {"device_enabled": True, "output_enabled": (True, True)}
                    old.load_config = lambda index, **kw: rig.hw.invoke("load_config", old)
                    old.set_channel_current = lambda channel, value, **kw: rig.hw.invoke("set_current", old)
                    old.set_channel_voltage = lambda channel, value, **kw: rig.hw.invoke("set_voltage", old)

                    def outputs(a, b, **kw):
                        assert (a, b) == (False, False)
                        rig.hw.invoke("set_outputs", old)
                        state["output_enabled"] = (a, b)

                    def enabled(value, **kw):
                        assert value is False
                        rig.hw.invoke("set_enabled", old)
                        state["device_enabled"] = value

                    old.set_output_enabled = outputs
                    old.set_device_enabled = enabled
                    old.collect_housekeeping = lambda **kw: rig.hw.invoke("read_off", old, result=dict(state))
                elif rig.family != "esi":
                    old.shutdown = lambda **kw: rig.hw.invoke("verified_shutdown", old, result=True)
                QTest.mouseClick(close_button, Qt.MouseButton.LeftButton)
                for _ in range(20):
                    app.processEvents()
                pending(initializing=True)
                assert rig.hw.count("close") == 0
                parent.grab().save(str(output / f"{slug}-cancel-pending.png"))
                rig.hw.finish_open()
                settle()
                assert not model.successes
                assert controller.device is None and not controller.initialized
                assert controller.main_state == parent.status.text() == "Disconnected", (
                    controller.main_state, parent.status.text(), model.messages)
                assert not parent.onAction.state and not parent.deviceOnAction.state
                assert not button.isChecked() and button.toolTip() == "Turn ON"
                assert not old._dll_port_claimed and not old._transport_poisoned
                assert rig.hw.count("open") == rig.hw.count("close") == 1
                parent.grab().save(str(output / f"{slug}-cancel-closed.png"))
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                settle()
                assert model.successes == [True] and len(model.created) == 2
                assert rig.disconnect(controller.device)
                parent.close()
                return 0
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            settle()
            pending()
            old = controller.device
            assert old is model.created[0] and old._dll_port_claimed
            assert [name for name, _ in rig.hw.operations] == ["open"]
            parent.grab().save(str(output / f"{slug}-connection-pending.png"))

            if finish != "retry":
                for _ in range(2):
                    QTest.mouseClick(close_button, Qt.MouseButton.LeftButton)
                    settle()
                    pending()
                assert rig.hw.count("close") == 0
                assert not any("shutdown could not be confirmed" in str(m).lower()
                               for m in model.messages)
                parent.grab().save(str(output / f"{slug}-close-pending.png"))

            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            settle()
            pending()
            assert model.created == [old], "A blocked opening must not be replaced by ON"
            assert rig.hw.count("close") == 0
            rig.hw.finish_open()
            app.processEvents()
            pending()
            assert rig.hw.count("open") == 1 and rig.hw.count("close") == 0

            if finish != "retry":
                for _ in range(2):
                    QTest.mouseClick(close_button, Qt.MouseButton.LeftButton)
                    settle()
                    assert controller.device is None
                    assert controller.main_state == parent.status.text() == "Disconnected", (
                        controller.main_state, parent.status.text())
                    assert not controller.initialized
                    assert not parent.onAction.state and not parent.deviceOnAction.state
                    assert not button.isChecked() and button.toolTip() == "Turn ON"
                assert rig.hw.count("open") == 1 and rig.hw.count("close") == 1
                assert not old._dll_port_claimed
                parent.grab().save(str(output / f"{slug}-closed.png"))

            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            settle()
            # Stop at successful transport establishment. Enabling HV after the
            # init-complete signal is deliberately outside this connection probe.
            assert model.successes == [True] and len(model.created) == 2
            assert controller.device is model.created[1] and controller.device.connected
            assert [name for name, owner in rig.hw.operations if owner == id(old)] == [
                "open", "close", "dispose"]
            assert old._transport_poisoned and old.thread_lock.locked()
            assert rig.disconnect(controller.device)
            parent.close()
        finally:
            rig.hw.open_release.set()
            rig.hw.close_release.set()
            rig.hw.baud_release.set()
            fixture.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "retry"))
