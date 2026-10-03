"""Polling loops hold the configured cadence and leave promptly on OFF."""

from __future__ import annotations

import time
import types

import pytest

import test_amx_hd_plugin_behavior
import test_amx_plugin_behavior
import test_dmmr_plugin_behavior

LOADERS = {
    "amx": test_amx_plugin_behavior._load_module,
    "amx_hd": test_amx_hd_plugin_behavior._load_hd_plugin_module,
    "dmmr": test_dmmr_plugin_behavior._load_module,
}


@pytest.fixture(params=sorted(LOADERS))
def module(request):
    return LOADERS[request.param]()


def _controller(interval_ms):
    return types.SimpleNamespace(acquiring=True, controllerParent=types.SimpleNamespace(interval=interval_ms))


def test_read_time_counts_toward_the_interval(module):
    controller = _controller(200)
    started = time.monotonic() - 0.15  # the read itself took 150 ms
    begin = time.monotonic()
    module._wait_for_next_poll(controller, started)
    assert time.monotonic() - begin == pytest.approx(0.05, abs=0.04)


def test_slow_read_still_leaves_a_short_gap_for_other_lock_users(module):
    controller = _controller(100)
    begin = time.monotonic()
    module._wait_for_next_poll(controller, begin - 1.0)
    assert module._POLL_MIN_GAP_S * 0.9 <= time.monotonic() - begin < 0.1


def test_off_ends_the_wait_without_waiting_for_the_interval(module, monkeypatch):
    controller = _controller(10_000)
    real_sleep = time.sleep

    def sleep_then_off(seconds):
        real_sleep(seconds)
        controller.acquiring = False

    monkeypatch.setattr(module.time, "sleep", sleep_then_off)
    begin = time.monotonic()
    module._wait_for_next_poll(controller, begin)
    assert time.monotonic() - begin < 2 * module._POLL_STOP_CHECK_S
