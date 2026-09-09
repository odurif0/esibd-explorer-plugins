"""Stateful PSU test double shared by behavioral and fault-injection tests."""

class StatefulPSU:
    """A controller whose setpoints, gates and interlocks can be read back."""

    def __init__(self):
        self.connected = True
        self.enabled = False
        self.outputs = (False, False)
        self.ranges = (False, False)
        self.interlocks = (False, False)
        self.voltages = {0: 0.0, 1: 0.0}
        self.currents = {0: 0.0, 1: 0.0}
        self.calls = []

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
