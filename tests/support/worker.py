"""The native-worker face the DMMR and AMX plugins require of a device (fakes and inline drivers)."""
from __future__ import annotations


class WorkerFace:
    """A device's calls behind the plugin's native-worker proxy (the DMMR and AMX plugins use only that).

    Each call goes to the device's method of the same name; ``closed`` follows its poisoned transport,
    and ``close`` refuses, as a worker still inside a DLL call does.
    """

    def __init__(self, family, device):
        self.family, self.session, self.device = family, f"{family}-test", device
        self.cancelled = 0

    @property
    def closed(self):
        return bool(getattr(self.device, "_transport_poisoned", False))

    def call_method(self, name, *args, rpc_timeout_s=None, _io_timeout_s=None, _cancel_event=None, **kwargs):
        return getattr(self.device, name)(*args, **kwargs)

    def cancel(self):
        self.cancelled += 1

    def close(self, grace_s=0):
        return False


def as_worker(family, device):
    """Give a fake or an inline driver the native-worker face the DMMR and AMX plugins require."""
    if family in ("dmmr", "amx", "amx_hd"):
        object.__setattr__(device, "_backend", WorkerFace(family, device))
        object.__setattr__(device, "_backend_mode", "process")
        if not hasattr(device, "_native_rpc_timeout_for"):
            object.__setattr__(device, "_native_rpc_timeout_for", lambda method, kwargs: 30.0)
    return device
