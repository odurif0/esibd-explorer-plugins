"""Fault injection through an actual native process, without instrument hardware."""
from __future__ import annotations

import json
import math
import select
import struct
import subprocess
import sys
import threading
import time

import pytest
from support.native import ROOT, TRANSPORT


def test_numpy_scalars_encode_as_their_python_values():
    np = pytest.importorskip("numpy")
    assert TRANSPORT.encode(np.bool_(True)) is True
    assert TRANSPORT.encode([np.bool_(False), np.int16(-3), np.float32(0.5)]) == [False, -3, 0.5]
    assert TRANSPORT.encode(np.float64("nan")) == {"$float": "nan"}
    assert TRANSPORT.encode(np.array(7)) == 7
    with pytest.raises(TypeError):
        TRANSPORT.encode(np.array([1, 2]))


@pytest.fixture
def worker(worker_binary, tmp_path):
    proxy = TRANSPORT.NativeWorkerProxy(ROOT, "test", {"name": "one"},
                                       command=[str(worker_binary), "--family", "test"],
                                       log_dir=tmp_path, stop_grace_s=.1)
    yield proxy
    proxy.close(grace_s=0)


@pytest.fixture
def raw_worker(worker_binary):
    process = subprocess.Popen([str(worker_binary), "--family", "test"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def _raw_send(process, value):
    payload = json.dumps(value).encode()
    process.stdin.write(struct.pack("!I", len(payload)) + payload)
    process.stdin.flush()


def _raw_initialize(process, config):
    _raw_send(process, {"version": 1, "id": 0, "op": "init", "family": "test", "config": config})
    assert select.select([process.stdout], [], [], 2)[0], "Missing initialization reply"
    assert TRANSPORT.receive(process.stdout)["success"]


class _BlockedLogSink:
    def __init__(self, stage, kind):
        self.parent = self
        self.stage = stage
        self.kind = kind
        self.entered = threading.Event()
        self.release = threading.Event()
        self.records = []
        self.filesystem_threads = []

    def _block(self, stage, kind=None):
        self.filesystem_threads.append(threading.get_ident())
        if stage == self.stage and (stage != "write" or kind == self.kind):
            self.entered.set()
            self.release.wait()

    def mkdir(self, **_kwargs):
        self._block("mkdir")

    def open(self, *_args, **_kwargs):
        self._block("open")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def write(self, line):
        record = json.loads(line)
        self._block("write", record["kind"])
        self.records.append(record)


@pytest.fixture
def blocked_log_sink(worker, monkeypatch):
    sinks = []

    def install(stage="write", kind="native_io"):
        assert worker._flush_logs(timeout_s=1)
        sink = _BlockedLogSink(stage, kind)
        sinks.append(sink)
        monkeypatch.setattr(worker, "_log_path", sink)
        return sink

    yield install
    for sink in sinks:
        sink.release.set()
    worker.close(grace_s=0)
    for thread in worker._threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in worker._threads)


def _native_hang_thread(worker, io_timeout):
    errors = []

    def run():
        try:
            worker.call("native_hang", rpc_timeout_s=30, _io_timeout_s=io_timeout)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


@pytest.mark.parametrize("log_dir", ["", ".", "relative/data"])
def test_relative_log_directory_fails_before_launch(monkeypatch, log_dir):
    def unexpected_launch(*_args, **_kwargs):
        pytest.fail("Invalid log directory launched a native worker")

    monkeypatch.setattr(TRANSPORT.subprocess, "Popen", unexpected_launch)
    with pytest.raises(ValueError, match="absolute Explorer data-directory"):
        TRANSPORT.NativeWorkerProxy(ROOT, "test", {}, log_dir=log_dir, command=["unused"])


def test_idle_watchdog_exits_with_undrained_stderr(raw_worker, worker_binary):
    other = TRANSPORT.NativeWorkerProxy(ROOT, "test", {}, command=[str(worker_binary), "--family", "test"])
    try:
        _raw_initialize(raw_worker, {"housekeeping_stderr_hang": True})
        assert raw_worker.wait(timeout=2) == 77
        assert len(raw_worker.stderr.read()) >= 4096
        assert other.call("echo", "independent") == "independent"
    finally:
        other.close(grace_s=0)


def test_parent_eof_exits_with_undrained_stderr(raw_worker):
    _raw_initialize(raw_worker, {})
    _raw_send(raw_worker, {"version": 1, "id": 1, "op": "call", "method": "stderr_hang",
                           "args": [], "kwargs": {}})
    assert select.select([raw_worker.stderr], [], [], 1)[0], "Fault did not begin writing stderr"
    time.sleep(.05)
    assert raw_worker.poll() is None
    raw_worker.stdin.close()
    assert raw_worker.wait(timeout=2) == 0
    assert len(raw_worker.stderr.read()) >= 4096


@pytest.mark.parametrize("stage", ["mkdir", "open", "write"])
def test_blocked_log_sink_cannot_delay_native_io_watchdog(worker, blocked_log_sink, stage):
    sink = blocked_log_sink(stage)
    thread, errors = _native_hang_thread(worker, .05)
    assert sink.entered.wait(timeout=1)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
    assert "TEST_Vendor_Hang" in str(errors[0])
    assert worker.closed and worker._process.poll() is not None
    assert not worker._pending
    assert worker._log_thread.is_alive() and worker._log_thread.daemon
    assert all(not thread.is_alive() for thread in worker._threads if thread is not worker._log_thread)
    assert set(sink.filesystem_threads) == {worker._log_thread.ident}


def test_blocked_log_sink_cannot_delay_close_or_other_worker(worker, blocked_log_sink, worker_binary):
    sink = blocked_log_sink()
    other = TRANSPORT.NativeWorkerProxy(ROOT, "test", {}, command=[str(worker_binary), "--family", "test"])
    # A request's records are written when it ends: a finished call stalls the sink.
    assert worker.call("native_wait", .001, _io_timeout_s=1.) is True
    thread, errors = _native_hang_thread(worker, 5.)
    closed = []
    closer = None
    try:
        assert sink.entered.wait(timeout=1)
        started = time.monotonic()
        assert worker.lifecycle_attribute("_transport_poisoned") is False
        assert time.monotonic() - started < .05
        closer = threading.Thread(target=lambda: closed.append(worker.close(grace_s=0)), daemon=True)
        started = time.monotonic()
        closer.start()
        closer.join(timeout=2)
        assert not closer.is_alive() and closed == [True]
        assert time.monotonic() - started < 2
        thread.join(timeout=1)
        assert not thread.is_alive() and errors
        assert worker._process.poll() is not None and not worker._pending
        assert worker._log_thread.is_alive()
        assert other.call("echo", "healthy", rpc_timeout_s=1) == "healthy"
    finally:
        sink.release.set()
        if closer is not None:
            closer.join(timeout=2)
        thread.join(timeout=2)
        other.close(grace_s=0)


def test_logger_queue_caps_drops_drains_and_stops(worker, blocked_log_sink):
    sink = blocked_log_sink(kind="stall")
    worker._log("stall")
    assert sink.entered.wait(timeout=1)
    started = time.monotonic()
    for index in range(512):
        worker._log("queued", index=index)
    assert time.monotonic() - started < 1
    assert worker._logs.qsize() == worker._logs.maxsize == 128
    assert worker._log_dropped > 0
    assert worker._log_bytes <= TRANSPORT.MAX_LOG_BYTES == 8 * 1024 * 1024
    started = time.monotonic()
    assert not worker._flush_logs(timeout_s=.01)
    assert time.monotonic() - started < .2
    assert worker.call("echo", "queue full", rpc_timeout_s=1) == "queue full"
    sink.release.set()
    deadline = time.monotonic() + 2
    while not worker._logs.empty() and time.monotonic() < deadline:
        time.sleep(.01)
    assert worker._flush_logs(timeout_s=1)
    worker._log("after_drop")
    assert worker._flush_logs(timeout_s=1)
    assert any(record["kind"] == "after_drop" and record.get("dropped_records", 0) > 0
               for record in sink.records)
    assert sum(record["kind"] == "queued" for record in sink.records) == 128
    assert set(sink.filesystem_threads) == {worker._log_thread.ident}
    assert worker.close(grace_s=0)
    assert all(not thread.is_alive() for thread in worker._threads)


def test_tagged_types_roundtrip(worker):
    value = (b"bytes", {1: (2., -math.inf)}, math.nan)
    reply = worker.call("echo", value)
    assert reply[:2] == value[:2]
    assert math.isnan(reply[2])
    worker.set_attribute("name", "two")
    assert worker.get_attribute("name") == "two"


def test_native_stdout_cannot_corrupt_protocol(worker):
    assert worker.call("stdout") is True
    assert worker.call("echo", "after noise") == "after noise"


def test_native_errors_remain_typed_and_recoverable(worker):
    with pytest.raises(ValueError, match="injected"):
        worker.call("fail")
    assert worker.call("echo", 4) == 4
    assert not worker.closed


def test_hung_worker_is_killed_and_reaped(worker):
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="safety is not confirmed"):
        worker.call("hang", rpc_timeout_s=.1)
    assert time.monotonic() - started < 3
    assert worker.closed
    assert worker._process.poll() is not None
    assert worker._pending == {}


def test_single_dll_watchdog_does_not_wait_for_batch_deadline(worker):
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="TEST_Vendor_Hang.*native I/O deadline"):
        worker.call("native_hang", rpc_timeout_s=300., _io_timeout_s=.05)
    assert time.monotonic() - started < 2
    assert worker._process.poll() is not None
    records = worker._log_path.read_text()
    assert '"symbol": "TEST_Vendor_Hang"' in records
    assert '"kind": "timeout"' in records


def test_late_native_return_cannot_keep_connection_alive(worker):
    with pytest.raises(TimeoutError, match="TEST_Vendor_Wait.*native I/O deadline"):
        worker.call("native_wait", .03, rpc_timeout_s=5, _io_timeout_s=.01)
    assert worker.closed and worker._process.poll() is not None


def test_fast_native_calls_have_matched_watchdog_frames(worker):
    for _ in range(5):
        assert worker.call("native_wait", .001, _io_timeout_s=1.) is True
    assert not worker.closed


def test_crash_does_not_affect_another_plugin(worker, worker_binary):
    other = TRANSPORT.NativeWorkerProxy(ROOT, "test", {}, command=[str(worker_binary), "--family", "test"])
    try:
        with pytest.raises(RuntimeError, match="pipe closed"):
            worker.call("crash")
        assert worker.closed
        assert worker._process.returncode == 73
        assert other.call("echo", "alive") == "alive"
    finally:
        other.close(grace_s=0)


def test_idle_housekeeping_hang_is_reaped_without_a_new_request(worker, worker_binary):
    other = TRANSPORT.NativeWorkerProxy(ROOT, "test", {}, command=[str(worker_binary), "--family", "test"])
    try:
        worker.set_attribute("housekeeping_hang", True)
        deadline = time.monotonic() + 3
        while worker._process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert worker._process.poll() == 77
        deadline = time.monotonic() + 1
        while (not worker.closed or worker._pending) and time.monotonic() < deadline:
            time.sleep(.01)
        assert worker.closed and not worker._pending
        assert other.call("echo", "still independent") == "still independent"
        assert worker._flush_logs(timeout_s=1)
        assert "TEST_Vendor_Idle_Hang" in worker._log_path.read_text()
    finally:
        other.close(grace_s=0)


def test_off_can_close_worker_during_active_call(worker):
    errors = []
    def operation():
        try:
            worker.call("hang", rpc_timeout_s=10)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=operation)
    thread.start()
    deadline = time.monotonic() + 1
    while worker._active is None and time.monotonic() < deadline:
        time.sleep(.005)
    started = time.monotonic()
    assert worker.close(grace_s=.05)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors
    assert time.monotonic() - started < 3


def test_cancellation_does_not_reuse_late_reply(worker):
    errors = []
    def operation():
        try:
            worker.call("wait", 5., rpc_timeout_s=10)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=operation)
    thread.start()
    deadline = time.monotonic() + 1
    while worker._active is None and time.monotonic() < deadline:
        time.sleep(.005)
    assert worker.cancel()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors and "cancelled" in str(errors[0])
    assert worker.call("echo", "new session request") == "new session request"


def test_parent_eof_terminates_even_blocked_native_call(worker_binary):
    import json
    import struct
    process = subprocess.Popen([str(worker_binary), "--family", "test"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def write(value):
        payload = json.dumps(value).encode()
        process.stdin.write(struct.pack("!I", len(payload)) + payload)
        process.stdin.flush()
    try:
        write({"version": 1, "id": 0, "op": "init", "family": "test", "config": {}})
        assert TRANSPORT.receive(process.stdout)["success"]
        write({"version": 1, "id": 1, "op": "call", "method": "hang", "args": [], "kwargs": {}})
        process.stdin.close()
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()


def test_logs_are_outside_plugin(worker):
    assert ROOT not in worker._log_path.parents
    worker.call("echo", 1)
    assert worker._flush_logs(timeout_s=1)
    records = worker._log_path.read_text()
    assert '"kind": "start"' in records
    assert '"kind": "request"' in records


def test_cancelled_queued_command_is_never_sent(worker):
    thread = threading.Thread(target=lambda: worker.call("wait", .3))
    thread.start()
    deadline = time.monotonic() + 1
    while worker._active is None and time.monotonic() < deadline:
        time.sleep(.005)
    before = worker._sequence
    cancel = threading.Event()
    timer = threading.Timer(.02, cancel.set)
    timer.start()
    try:
        with pytest.raises(RuntimeError, match="cancelled before dispatch"):
            worker.call("echo", "must not run", cancel_event=cancel)
        assert worker._sequence == before
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert worker.call("echo", "still healthy") == "still healthy"
    finally:
        timer.join(timeout=1)


def test_local_callbacks_run_on_caller_not_protocol_thread(worker):
    reports = []
    caller = threading.get_ident()
    assert worker.call("progress", {"discharge": 10.}, on_discharge=lambda value: reports.append((value, threading.get_ident())))
    assert reports == [({"discharge": 10.}, caller)]


def test_psu_cancel_alias_remains_local(worker):
    assert worker.call("echo", "healthy", cancel=threading.Event()) == "healthy"
    cancel = threading.Event()
    cancel.set()
    before = worker._sequence
    with pytest.raises(RuntimeError, match="cancelled before dispatch"):
        worker.call("echo", "not sent", cancel=cancel)
    assert worker._sequence == before


def test_lifecycle_reads_never_queue_behind_a_hung_worker(worker):
    errors = []
    def run():
        try:
            worker.call("hang", rpc_timeout_s=10)
        except RuntimeError as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1
    while worker._active is None and time.monotonic() < deadline:
        time.sleep(.005)
    started = time.monotonic()
    assert worker.lifecycle_attribute("connected") is False
    assert worker.lifecycle_attribute("_transport_poisoned") is False
    assert time.monotonic() - started < .05
    worker.close(grace_s=0)
    thread.join(timeout=2)
    assert errors and not thread.is_alive()
    assert worker.lifecycle_attribute("_transport_poisoned") is True
    assert worker.lifecycle_attribute("_dll_port_claimed") is False


BAD_REPLY_SCRIPT = r'''
import json, struct, sys, time


def read():
    n, = struct.unpack('!I', sys.stdin.buffer.read(4))
    return json.loads(sys.stdin.buffer.read(n))


def send(value):
    data = json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack('!I', len(data)) + data)
    sys.stdout.buffer.flush()
request = read()
send({'version': 1, 'id': request['id'], 'kind': 'reply', 'success': True,
      'value': {'family': 'test', 'protocol': 1}})
request = read()
reply = json.loads(sys.argv[1])
reply.setdefault('id', request['id'])
if reply.pop('_unmatched_io', False):
    send({'version': 1, 'id': request['id'], 'kind': 'native_io', 'phase': 'enter',
          'sequence': 0, 'symbol': 'TEST_Vendor_Unfinished', 'timeout_s': .01})
send(reply)
time.sleep(30)
'''


@pytest.mark.parametrize("bad", [
    {"version": 1, "kind": "reply", "success": True},
    {"version": 1, "kind": "reply", "success": True, "value": {"$float": "invalid"}},
    {"version": 1, "kind": "reply", "success": False, "error": "not an object"},
    {"version": 1, "id": 100, "kind": "reply", "success": True, "value": 1},
    {"version": True, "kind": "reply", "success": True, "value": 1},
    {"version": 1, "kind": "reply", "success": True, "value": 1, "state": {"connected": "yes"}},
    {"version": 1, "kind": "reply", "success": True, "value": True, "_unmatched_io": True},
])


def test_malformed_response_retires_and_reaps_process(tmp_path, bad):
    import json
    proxy = TRANSPORT.NativeWorkerProxy(ROOT, "test", {}, log_dir=tmp_path, stop_grace_s=.1,
                                       command=[sys.executable, "-u", "-c", BAD_REPLY_SCRIPT, json.dumps(bad)])
    try:
        with pytest.raises(RuntimeError, match="unusable"):
            proxy.call("echo", 1)
        assert proxy.closed and proxy._process.poll() is not None
        assert proxy._pending == {}
    finally:
        proxy.close(grace_s=0)


def test_log_segments_rotate_keeping_the_first_and_the_newest(worker, monkeypatch):
    monkeypatch.setattr(TRANSPORT, "MAX_LOG_BYTES", 1024)
    monkeypatch.setattr(TRANSPORT, "MAX_LOG_SEGMENTS", 3)
    first = worker._log_base
    for index in range(40):
        worker._log("large", index=index, message="x" * 400)
    assert worker._flush_logs(timeout_s=1)
    files = sorted(first.parent.glob(f"{first.stem}*.jsonl"))
    assert first in files and len(files) == 3
    assert all(path.stat().st_size < 2 * 1024 for path in files)
    newest = [json.loads(line) for line in worker._log_path.read_text().splitlines()]
    assert newest[0]["kind"] == "log_segment" and newest[0]["removed"]
    assert newest[-1]["index"] == 39
    assert '"kind": "start"' in first.read_text(), "the session start is never rotated away"


def _finished(worker, key, *, routine=True, elapsed=.01, symbols=(("COM_X_GetState", .005, 0, [0]),)):
    pending = TRANSPORT._Pending()
    pending.log_key, pending.log_routine = key, routine
    pending.log_started = time.monotonic() - elapsed
    with worker._state_lock:
        worker._buffer_log(pending, "request", first=True, request=1, operation="call", method=key)
        for symbol, seconds, status, values in symbols:
            frame = {"phase": "exit", "symbol": symbol, "elapsed_s": seconds, "status": status, "values": values}
            worker._buffer_log(pending, "native_io", request=1, **frame)
            worker._note_native_outcome(pending, symbol, seconds, frame)
    worker._finish_request_log(pending, 1, "reply")


def _records(worker):
    assert worker._flush_logs(timeout_s=1)
    return [json.loads(line) for line in worker._log_path.read_text().splitlines()]


def test_routine_reads_are_summarized_unless_slow_or_changed(worker, monkeypatch):
    clock = [time.monotonic()]
    monkeypatch.setattr(TRANSPORT.time, "monotonic", lambda: clock[0])
    worker._log_window_start = clock[0]
    for _ in range(50):
        _finished(worker, "call:get_state")
    _finished(worker, "call:get_state", elapsed=1.)  # slow
    _finished(worker, "call:get_state", symbols=(("COM_X_GetState", .005, 0, [32773]),))  # state changed
    _finished(worker, "call:get_state", symbols=(("COM_X_GetState", .005, -10, []),))  # receive error
    for _ in range(15):
        _finished(worker, "call:set_voltage", routine=False)
    clock[0] += TRANSPORT.LOG_SUMMARY_S
    _finished(worker, "call:get_state")
    records = _records(worker)
    replies = [record for record in records if record["kind"] == "reply" and record["request"] == 1]
    assert len(replies) == 1 + 3 + TRANSPORT.LOG_FULL_CALLS_PER_WINDOW + 1
    summary = next(record for record in records if record["kind"] == "summary")
    assert summary["calls"]["call:get_state"][0] == 49
    assert summary["calls"]["call:set_voltage"][0] == 15 - TRANSPORT.LOG_FULL_CALLS_PER_WINDOW
    assert summary["native_io"]["COM_X_GetState"][0] == 49 + 5
    changed = [record for record in records if record["kind"] == "native_io" and record.get("values") == [32773]]
    assert changed, "a changed discrete output is written in full"


def test_esi_conversion_ready_flags_are_not_a_state_change(worker, monkeypatch):
    # 2026-10-22 lab: GetCompleteState index 7 toggles 223 <-> 255 on every 1 s poll.
    clock = [time.monotonic()]
    monkeypatch.setattr(TRANSPORT.time, "monotonic", lambda: clock[0])
    worker._log_window_start = clock[0]
    state = lambda ready, modules=32768: (("COM_ESI_CTRL_GetCompleteState", .003, 0,
                                           [15, 0, 55, 0, 15, 61452, 0, [15, ready, ready, 0, 12],
                                            [33024, 49408, modules, 0, 32768]]),)
    for second in range(30):
        _finished(worker, "call:collect_diagnostics", symbols=state(223 if second % 2 else 255))
    _finished(worker, "call:collect_diagnostics", symbols=state(223, modules=16384))  # a module state changed
    clock[0] += TRANSPORT.LOG_SUMMARY_S
    _finished(worker, "call:collect_diagnostics", symbols=state(255, modules=16384))
    records = _records(worker)
    replies = [record for record in records if record["kind"] == "reply" and record["request"] == 1]
    assert len(replies) == 1 + 1 + 1, len(replies)
    assert next(record for record in records if record["kind"] == "summary")["calls"]["call:collect_diagnostics"][0] == 29


def test_twelve_hours_of_a_polling_ampr_fit_in_the_first_segment(worker, monkeypatch):
    """One 12-module cycle per second: get_state, 11 measured reads, one setpoint readback."""
    clock = [time.monotonic()]
    monkeypatch.setattr(TRANSPORT.time, "monotonic", lambda: clock[0])
    worker._log_window_start = clock[0]
    written = []
    # Count what would be written; the simulated hour outruns the bounded writer queue.
    monkeypatch.setattr(worker, "_emit", lambda record: written.append(len(json.dumps(record)) + 1))
    measured = (("COM_AMPR_12_GetMeasuredModuleOutputVoltages", .06, 0, [[1.5, 2.5, 3.5, 4.5]]),)
    readback = tuple(("COM_AMPR_12_GetModuleOutputVoltage", .03, 0, [12.5]) for _ in range(4))
    hour = 3600
    for second in range(hour):
        _finished(worker, "call:get_state", symbols=(("COM_AMPR_12_GetState", .015, 0, [0]),))
        for _ in range(11):
            _finished(worker, "call:get_all_module_voltage_measured", symbols=measured)
        _finished(worker, "call:get_module_voltages", symbols=measured + readback, elapsed=.19)
        clock[0] += 1.
    twelve_hours = 12 * sum(written)
    assert twelve_hours < TRANSPORT.MAX_LOG_BYTES, twelve_hours


def test_abi_generated_sources_are_current():
    result = subprocess.run(["python3", str(ROOT / "tools/generate_native_abi.py"), "--check"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
