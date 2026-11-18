"""Stateful PSU test double shared by behavioral and fault-injection tests."""
from copy import deepcopy

CHANNELS = (0, 1)


class _Worker:
    """The native-worker proxy of a StatefulPSU: each operation goes to the fake's method of that name."""

    session = "psu-test"
    closed = False

    def __init__(self, device):
        self.device = device

    def call_method(self, name, *args, rpc_timeout_s=None, _io_timeout_s=None, _cancel_event=None, **kwargs):
        if name == "apply_manual_state":
            kwargs["cancel"] = _cancel_event
        return getattr(self.device, name)(*args, **kwargs)

    def close(self, grace_s=0):
        return False  # A live worker is not reaped by a close request.


class StatefulPSU:
    """A controller whose setpoints, gates and interlocks can be read back.

    The plugin sends a manual state to its worker as one operation (``apply_manual_state``); this fake
    gives that operation's end state only. Its checks, ramps and recoveries are tested on the worker
    itself (native/worker/tests/psu_controller.rs).
    """

    _backend_mode = "process"

    def __init__(self):
        self.connected = True
        self.enabled = False
        self.outputs = (False, False)
        self.ranges = (False, False)
        self.interlocks = (False, False)
        self.voltages = {0: 0.0, 1: 0.0}
        self.currents = {0: 0.0, 1: 0.0}
        self.calls = []
        self.requests = []  # The manual states the plugin sent to its worker.
        self.apply_failure = None  # Raised by the next apply, as the worker reports a refused state.
        self._backend = _Worker(self)

    def apply_manual_state(self, manual_state, *, cancel=None, interlock_monitoring=True, **kwargs):
        self.requests.append(deepcopy(manual_state))
        if self.apply_failure is not None:
            failure, self.apply_failure = self.apply_failure, None
            raise failure

        def cancelled():
            return cancel is not None and cancel.is_set()
        voltages = {int(ch): float(v) for ch, v in (manual_state.get("voltage_values") or {}).items()}
        currents = {int(ch): float(v) for ch, v in (manual_state.get("current_limit_values") or {}).items()}
        if not manual_state.get("setpoints_only"):
            outputs = tuple(bool((manual_state.get("output_enabled") or {}).get(ch, False)) for ch in CHANNELS)
            ranges = tuple(bool((manual_state.get("full_range_enabled") or {}).get(ch, False)) for ch in CHANNELS)
            if ranges != tuple(self.ranges) or tuple(self.outputs) != outputs or self.enabled != any(outputs):
                self.set_output_enabled(False, False)
                if ranges != tuple(self.ranges):
                    self.set_output_full_range(*ranges)
                for ch in CHANNELS:
                    if cancelled():
                        return None
                    self.set_channel_voltage(ch, voltages.get(ch, 0.0))
                    self.set_channel_current(ch, currents.get(ch, 0.0))
                if any(outputs) and tuple(self.interlocks) != (interlock_monitoring,) * 2:
                    self.set_interlock_enabled(interlock_monitoring, interlock_monitoring)
                self.set_device_enabled(any(outputs))
                if any(outputs) and not cancelled():
                    self.set_output_enabled(*outputs)
                return None
        for ch, value in currents.items():
            if not cancelled() and self.currents[ch] != value:
                self.set_channel_current(ch, value)
        for ch, value in voltages.items():
            if not cancelled() and self.voltages[ch] != value:
                self.set_channel_voltage(ch, value)
        return None

    def connect(self, **kwargs):
        self.connected = True
        return True

    def set_channel_voltage(self, channel, value, **kwargs):
        assert self.connected
        self.voltages[channel] = value
        self.calls.append(("voltage", channel, value))

    def set_channel_current(self, channel, value, **kwargs):
        assert self.connected
        self.currents[channel] = value
        self.calls.append(("current", channel, value))

    def set_output_enabled(self, *enabled, **kwargs):
        assert self.connected
        self.outputs = enabled
        self.calls.append(("outputs", *enabled))

    def set_device_enabled(self, enabled, **kwargs):
        assert self.connected
        self.enabled = enabled
        self.calls.append(("global", enabled))

    def set_output_full_range(self, *enabled, **kwargs):
        self.ranges = enabled
        self.calls.append(("range", *enabled))

    def set_interlock_enabled(self, *enabled, **kwargs):
        self.interlocks = enabled
        self.calls.append(("interlocks", *enabled))

    def get_interlock_enabled(self, **kwargs):
        return self.interlocks

    def get_channel_voltage_limits(self, channel, **kwargs):
        return self.voltages[channel], 10000.0

    def get_channel_current_limits(self, channel, **kwargs):
        return self.currents[channel], 1.0

    def get_channel_measured_voltage(self, channel, **kwargs):
        return self.voltages[channel] if self.outputs[channel] else 0.0

    def get_output_full_range(self, **kwargs):
        return self.ranges

    def get_output_enabled(self, **kwargs):
        return self.outputs

    def get_device_enabled(self, **kwargs):
        return self.enabled

    def collect_housekeeping(self, **kwargs):
        return {"device_enabled": self.enabled, "output_enabled": self.outputs, "channels": []}

    def disconnect(self, **kwargs):
        self.connected = False
        self.calls.append(("disconnect",))
        return True

    def close(self):
        pass
