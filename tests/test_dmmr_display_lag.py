"""Explorer's display-lag pauses count DMMR errors silently; the plugin must name them.

Lab log 2026-10-21 16:20-16:24: "Error count is 5 ... 25" every ~5 s, then the DMMR
communication closed, while the DMMR protocol log had no error at all.
"""
from __future__ import annotations

from types import SimpleNamespace

from support import explorer


def test_display_lag_pause_is_reported_once_per_episode_with_the_lagging_device():
    pauses = iter([True, True, False, True])

    class LaggingDevice(explorer.Device):
        def bufferLagging(self, wait=None):
            return next(pauses)

    module = explorer.load_plugin("dmmr/dmmr_plugin.py", "dmmr_display_lag_test", runtimes=("dmmr",),
                                  Device=LaggingDevice)
    device = object.__new__(module.DMMRDevice)
    slow = SimpleNamespace(name="AMPR_A", lagging_seconds=14)
    device.pluginManager = SimpleNamespace(
        DeviceManager=SimpleNamespace(getDevices=lambda: [SimpleNamespace(name="DMMR", lagging_seconds=0), slow]),
        Settings=SimpleNamespace(maxLagTolerance=10, lagWaitTime=5))
    device.controller = SimpleNamespace(maxErrorCount=25)
    logs = []
    device.print = lambda message, flag=None: logs.append((message, flag))

    assert [device.bufferLagging() for _ in range(4)] == [True, True, False, True]

    assert len(logs) == 2, "one warning per lag episode, not per pause"
    message, flag = logs[0]
    assert flag == module.PRINT.WARNING
    assert "AMPR_A is 14 s behind" in message and "after 25" in message
    assert "DMMR link itself reported no error" in message
