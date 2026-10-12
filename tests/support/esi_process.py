"""ESI process-worker fakes shared by the recovery tests."""
from pathlib import Path

WORKER = Path(__file__).resolve().parents[2] / "esi/vendor/runtime/esi/_process.py"
FAKE_CONTROLLER = '''
import os
import time


class Controller:
    def __init__(self, **kwargs):
        self.counter = 0
        self._transport_poisoned = False
        self.connected = True

    def ping(self):
        print("vendor output must not corrupt RPC", flush=True)
        os.write(1, b"native vendor output\\n")
        return (b"config bytes", {1: (True, 700.)})

    def add(self, amount):
        value = self.counter
        time.sleep(.01)
        self.counter = value + amount
        return self.counter

    def block(self, *, on_discharge=None, cancel_event=None):
        if on_discharge:
            on_discharge({"entered": True})
        if cancel_event is not None:
            return cancel_event.wait(5.)
        time.sleep(60.)

    def disconnect(self, **kwargs):
        raise RuntimeError("get_enable failed: -13 (Wrong command received)")

    def collect_diagnostics(self, **kwargs):
        raise RuntimeError("get_enable failed: -13 (Wrong command received)")

    def crash(self):
        os._exit(7)

    def poison(self):
        self._transport_poisoned = True
        raise RuntimeError("ESI DLL call timed out")
'''
