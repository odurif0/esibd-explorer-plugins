"""Small helper controllers used to exercise process isolation in tests."""

from __future__ import annotations

import time


class DummyController:
    """Small controller used to validate process-isolated round trips."""

    def __init__(self, start: int = 0):
        self.value = start
        self._transport_poisoned = False

    def add(self, delta: int) -> int:
        self.value += delta
        return self.value

    def get_value(self) -> int:
        return self.value

    def explode(self):
        raise ValueError("boom")

    def poison(self):
        self._transport_poisoned = True
        raise RuntimeError("controller poisoned")

    def sleep(self, seconds: float):
        time.sleep(seconds)
        return seconds
