"""Native TPG366 adapters, actual Linux PTYs, and bounded worker lifecycles."""

import ast
import errno
import importlib.util
import json
import math
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from test_tpg366_protocol import FakeSerial, frame, protocol

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'tpg366'


def load_runtime(name):
    spec = importlib.util.spec_from_file_location(f'tpg366_native_test{name}', PLUGIN / '_runtime' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


adapter = load_runtime('_native_link')


class FakeWorker:
    def __init__(self, plugin_root, family, config, *, log_dir):
        assert Path(plugin_root) == PLUGIN and family == 'tpg366'
        json.dumps(config)
        self.config, self.log_dir = config, log_dir
        self.closed, self.cancel_count = False, 0
        self.calls, self.failure, self.after_call = [], None, None
        self.close_result = True

    def call_method(self, method, *, rpc_timeout_s, _cancel_event, on_progress):
        self.calls.append((method, rpc_timeout_s, _cancel_event))
        on_progress({'family': 'tpg366', 'nak_count': 3})
        if self.after_call:
            self.after_call()
        if self.failure:
            raise self.failure
        if method == 'initialize':
            return {'identification': 'TPG366,x,x,x,x', 'gauges': ('TPR',) * 6, 'unit': 0}
        assert method == 'read_pressures'
        return {'pressures': (1., math.nan, -1., 2., 3., 4.), 'statuses': (0, 1, 0, 0, 0, 0),
                'unit': 'mbar', 'received_at': 42.}

    def get_attribute(self, *args, **kwargs):
        raise AssertionError('An adapter failure/finally must not issue a second RPC')

    def cancel(self):
        self.cancel_count += 1

    def close(self, *, grace_s):
        assert grace_s == 0
        self.closed = self.close_result
        return self.close_result


@pytest.fixture
def fake_link(tmp_path):
    return adapter.NativeLink(FakeWorker, protocol, PLUGIN, 'COM42', 9600, threading.Event(), log_dir=tmp_path)


def test_adapter_preserves_identity_readings_budgets_and_local_event(fake_link):
    fake_link.initialize()
    reading = fake_link.read_pressures()
    assert fake_link.identification.startswith('TPG366,') and len(fake_link.gauges) == 6
    assert fake_link.unit == 0 and fake_link.nak_count == 3
    assert reading.pressures[0] == 1. and math.isnan(reading.pressures[1]) and reading.pressures[2] == -1.
    assert reading.statuses == (0, 1, 0, 0, 0, 0) and reading.received_at == 42.
    assert fake_link.proxy.config == {'port': 'COM42', 'baudrate': 9600}
    assert fake_link.proxy.calls == [('initialize', 10., fake_link.cancelled), ('read_pressures', 5., fake_link.cancelled)]
    fake_link.cancel()
    assert fake_link.proxy.cancel_count == 1
    fake_link.close()
    assert not fake_link.is_open


def test_failed_transaction_reports_naks_without_a_followup_getattr_rpc(fake_link):
    failure = RuntimeError('PRX [waiting for ACK]: TPG366 returned NAK to 3 transmissions')
    failure.native_error_kind = 'ProtocolError'
    fake_link.proxy.failure = failure
    with pytest.raises(protocol.ProtocolError, match='3 transmissions'):
        fake_link.read_pressures()
    assert fake_link.nak_count == 3 and len(fake_link.proxy.calls) == 1


def test_worker_failure_is_not_misclassified_as_recoverable_serial_protocol(fake_link):
    fake_link.proxy.failure = RuntimeError('Native worker exited')
    with pytest.raises(RuntimeError, match='worker exited'):
        fake_link.read_pressures()
    assert fake_link.nak_count == 3


def test_cancelled_before_dispatch_or_during_reply_never_publishes_a_sample(fake_link):
    fake_link.cancelled.set()
    with pytest.raises(protocol.Cancelled):
        fake_link.read_pressures()
    assert not fake_link.proxy.calls
    fake_link.cancelled.clear()
    fake_link.proxy.after_call = fake_link.cancelled.set
    with pytest.raises(protocol.Cancelled):
        fake_link.read_pressures()
    assert len(fake_link.proxy.calls) == 1


def test_nak_progress_is_local_monotonic_and_type_checked(fake_link):
    fake_link._progress({'family': 'tpg366', 'nak_count': 2})
    for value in [{'family': 'other', 'nak_count': 9}, {'family': 'tpg366', 'nak_count': 1},
                  {'family': 'tpg366', 'nak_count': True}, {'family': 'tpg366', 'nak_count': '9'}, None]:
        fake_link._progress(value)
    assert fake_link.nak_count == 2


def test_close_requires_confirmed_process_exit_not_a_cached_closed_flag(fake_link):
    fake_link.proxy.close_result = False
    with pytest.raises(OSError, match='unconfirmed'):
        fake_link.close()
    assert fake_link.is_open
    fake_link.proxy.close_result = True
    fake_link.proxy._process = SimpleNamespace(poll=lambda: None)
    with pytest.raises(OSError, match='unconfirmed'):
        fake_link.close()
    assert fake_link.proxy.closed and fake_link.is_open
    fake_link.proxy._process = SimpleNamespace(poll=lambda: -15)
    fake_link.close()
    assert not fake_link.is_open


@pytest.fixture(scope='module')
def native_executable():
    if not sys.platform.startswith('linux'):
        pytest.skip('Actual PTY integration requires Linux')
    target = ROOT / 'native/target/tpg366-tests'
    result = subprocess.run(['cargo', 'build', '--offline', '--manifest-path', str(ROOT / 'native/Cargo.toml'),
                             '--target-dir', str(target), '--no-default-features', '--features', 'tpg366',
                             '--bin', 'esibd-native-worker'], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    executable = target / 'debug/esibd-native-worker'
    assert executable.is_file()
    return executable


class PtyInstrument:
    """Feed the reference byte engine through a real PTY, including transport faults."""

    def __init__(self):
        import tty
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.name = os.ttyname(self.slave)
        self.instrument = FakeSerial()
        self.lock, self.stop, self.started = threading.RLock(), threading.Event(), threading.Event()
        self.errors, self.pending = [], []
        self.naks, self.bad_ack, self.data_override = {}, {}, {}
        self.hold_ack, self.hold_data, self.delay_ack = None, None, {}
        self.change_unit_on_nak = False
        self.thread = threading.Thread(target=self._run, name='TPG366 PTY instrument')
        self.thread.start()

    @property
    def writes(self):
        with self.lock:
            return list(self.instrument.writes)

    def _send(self, data):
        # Small fragments exercise serial reads without an artificial worker timeout.
        for offset in range(0, len(data), 2):
            if self.stop.is_set():
                return
            os.write(self.master, data[offset:offset + 2])
            time.sleep(.0002)

    def _token(self, token):
        with self.lock:
            serial = self.instrument
            if token == b'\x03' and self.slave is not None:
                # Only the worker owns the open slave after its first ETX.
                os.close(self.slave)
                self.slave = None
            serial.ack = b'\x06\r\n'
            if token in self.naks and self.naks[token]:
                self.naks[token] -= 1
                serial.ack = b'\x15\r\n'
                if self.change_unit_on_nak:
                    serial.responses['UNI'] = '1'
            if token in self.bad_ack:
                serial.ack = self.bad_ack.pop(token)
            serial.write(token)
            reply = bytes(serial.buffer)
            serial.buffer.clear()
            if token == self.hold_ack or (token == b'\x05' and serial.command == self.hold_data):
                self.started.set()
                reply = b''
            elif token in self.delay_ack:
                self.pending.append((time.monotonic() + self.delay_ack.pop(token), reply))
                self.started.set()
                reply = b''
            elif token == b'\x05' and serial.command in self.data_override:
                reply = self.data_override.pop(serial.command)
        self._send(reply)

    def _run(self):
        command = bytearray()
        try:
            while not self.stop.is_set():
                with self.lock:
                    due = [item for item in self.pending if item[0] <= time.monotonic()]
                    self.pending = [item for item in self.pending if item not in due]
                for _deadline, reply in due:
                    self._send(reply)
                readable, _, _ = select.select([self.master], [], [], .005)
                if not readable:
                    continue
                try:
                    data = os.read(self.master, 1024)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    time.sleep(.005)  # No slave is open between worker connections.
                    continue
                for byte in data:
                    if byte in (3, 5) and not command:
                        self._token(bytes([byte]))
                    else:
                        command.append(byte)
                        if byte == 13:
                            self._token(bytes(command))
                            command.clear()
        except Exception as exc:
            self.errors.append(exc)

    def close(self):
        self.stop.set()
        self.thread.join(2)
        os.close(self.master)
        if self.slave is not None:
            os.close(self.slave)
        assert not self.thread.is_alive()
        assert not self.errors, self.errors


@pytest.fixture
def native_rig(native_executable, tmp_path):
    transport = load_runtime('_native_worker')
    serial = PtyInstrument()
    links = []
    proxies = []
    harnesses = []
    log_dir = tmp_path / 'explorer-data/logs/tpg366'

    def worker_class(plugin_root, family, config, *, log_dir):
        proxy = transport.NativeWorkerProxy(plugin_root, family, {**config, 'timeout_s': .2, 'etx_settle_s': .02},
                                            log_dir=log_dir, stop_grace_s=.15,
                                            command=[str(native_executable), '--family', 'tpg366'])
        proxies.append(proxy)
        return proxy

    def new_link(stop=None):
        link = adapter.NativeLink(worker_class, protocol, PLUGIN, serial.name, 9600,
                                  stop or threading.Event(), log_dir=log_dir)
        links.append(link)
        return link

    rig = SimpleNamespace(serial=serial, new_link=new_link, links=links, log_dir=log_dir, worker_class=worker_class,
                          proxies=proxies, harnesses=harnesses, data_path=tmp_path / 'explorer-data')
    try:
        yield rig
    finally:
        for harness in harnesses:
            harness.stop.set()
            if harness.host._native_link is not None:
                harness.host._native_link.cancel()
            harness.thread.join(2)
            assert not harness.thread.is_alive()
        for link in links:
            link.cancelled.set()
            link.cancel()
            link.close()
            assert link.proxy._process.poll() is not None
            assert all(not thread.is_alive() for thread in link.proxy._threads)
        for proxy in proxies:
            proxy.close(grace_s=0)
            assert proxy._process.poll() is not None and all(not thread.is_alive() for thread in proxy._threads)
        serial.close()


def assert_reaped(link):
    link.close()
    assert not link.is_open and link.proxy._process.poll() is not None
    assert all(not thread.is_alive() for thread in link.proxy._threads)


@pytest.mark.parametrize('unit,factor', [(0, 1.), (1, 1013.25 / 760.), (2, .01), (3, 1013.25 / 760000.), (4, 1.)])
def test_actual_native_pty_units_statuses_and_read_only_commands(native_rig, unit, factor):
    rig = native_rig
    with rig.serial.lock:
        rig.serial.instrument.responses['UNI'] = str(unit)
        rig.serial.instrument.responses['PRX'] = frame(statuses=(0, 1, 2, 3, 4, 6), values=['-1'] * 6)
    link = rig.new_link()
    link.initialize()
    reading = link.read_pressures()
    assert reading.pressures[0] == pytest.approx(-factor)
    assert all(math.isnan(value) for value in reading.pressures[1:])
    assert reading.statuses == (0, 1, 2, 3, 4, 6)
    assert len(link.gauges) == 6 and math.isfinite(reading.received_at)
    assert rig.serial.writes == [b'\x03', b'AYT\r', b'\x05', b'TID\r', b'\x05', b'UNI\r', b'\x05',
                                 b'PRX\r', b'\x05', b'UNI\r', b'\x05']
    assert link.proxy._log_path.is_relative_to(rig.log_dir)
    assert_reaped(link)


@pytest.mark.parametrize('tail', [b'\n', b'partial pressure\r\n', b'\r\n\r'])
def test_actual_native_pty_startup_stale_tail(native_rig, tail):
    with native_rig.serial.lock:
        native_rig.serial.instrument.before_ack = tail
    link = native_rig.new_link()
    link.initialize()
    assert link.identification.startswith('TPG366,') and math.isfinite(link.read_pressures().pressures[0])


def test_actual_native_pty_nak_failure_and_resync_reuse_port_and_keep_count(native_rig):
    rig = native_rig
    link = rig.new_link()
    link.initialize()
    process = link.proxy._process
    identity, unit = link.identification, link.unit
    with rig.serial.lock:
        rig.serial.naks[b'PRX\r'] = 3
    started = time.monotonic()
    with pytest.raises(protocol.ProtocolError, match='3 transmissions'):
        link.read_pressures()
    assert time.monotonic() - started < 1. and link.nak_count == 3 and link.is_open
    link.initialize()
    assert link.proxy._process is process and process.poll() is None
    assert link.nak_count == 3  # Reopening a fresh Link would lose this per-ON counter.
    assert (link.identification, link.unit) == (identity, unit)
    assert math.isfinite(link.read_pressures().pressures[0])
    assert rig.serial.writes.count(b'\x03') == 2 and rig.serial.writes.count(b'PRX\r') == 4
    requests = [json.loads(line) for line in link.proxy._log_path.read_text().splitlines()]
    assert not any(item.get('operation') == 'getattr' for item in requests)


@pytest.mark.parametrize('fault,phase', [('partial_ack', 'waiting for ACK'), ('partial_data', 'waiting for data'),
                                       ('late_ack', 'waiting for ACK')])
def test_actual_native_pty_timeout_does_not_retransmit_and_explicit_resync_recovers(native_rig, fault, phase):
    rig = native_rig
    link = rig.new_link()
    link.initialize()
    with rig.serial.lock:
        if fault == 'partial_ack':
            rig.serial.bad_ack[b'PRX\r'] = b'\x06\r'
        elif fault == 'partial_data':
            rig.serial.data_override['PRX'] = b'0,1\r'
        else:
            rig.serial.delay_ack[b'PRX\r'] = .3
    with pytest.raises(protocol.ProtocolError, match=f'PRX \\[{phase}\\]'):
        link.read_pressures()
    assert rig.serial.writes.count(b'PRX\r') == 1 and link.nak_count == 0
    if fault == 'late_ack':
        time.sleep(.15)  # Let the old ACK arrive before the explicit ETX/purge.
    link.initialize()
    assert math.isfinite(link.read_pressures().pressures[0])
    assert rig.serial.writes.count(b'PRX\r') == 2


@pytest.mark.parametrize('reply,match', [(b'\xff\r\n', 'Non-ASCII'), (b'x' * 513, '512 bytes')])
def test_actual_native_pty_corrupt_reply_is_not_a_sample(native_rig, reply, match):
    link = native_rig.new_link()
    link.initialize()
    with native_rig.serial.lock:
        native_rig.serial.data_override['PRX'] = reply
    with pytest.raises(protocol.ProtocolError, match=match):
        link.read_pressures()


@pytest.mark.parametrize('value', ['NaN', 'inf', '1E999'])
def test_actual_native_pty_nonfinite_pressure_and_changed_unit_are_fatal(native_rig, value):
    link = native_rig.new_link()
    link.initialize()
    with native_rig.serial.lock:
        native_rig.serial.instrument.responses['PRX'] = frame(values=[value] * 6)
    with pytest.raises(protocol.ProtocolError, match='Non-finite'):
        link.read_pressures()
    with native_rig.serial.lock:
        native_rig.serial.instrument.responses['PRX'] = frame()
        native_rig.serial.instrument.responses['UNI'] = '1'
    with pytest.raises(protocol.ProtocolError, match='unit changed'):
        link.read_pressures()
    assert link.unit == 4


def test_actual_native_pty_failed_initialization_retains_handle_until_close(native_rig):
    link = native_rig.new_link()
    with native_rig.serial.lock:
        native_rig.serial.instrument.responses['AYT'] = 'TPG362,x,x,x,x'
    with pytest.raises(protocol.ProtocolError, match='identify a TPG366'):
        link.initialize()
    assert link.is_open and link.proxy.get_attribute('connected') is True
    with native_rig.serial.lock:
        native_rig.serial.instrument.responses['AYT'] = 'TPG366,x,x,x,x'
    link.initialize()
    assert math.isfinite(link.read_pressures().pressures[0])
    assert_reaped(link)


def test_actual_native_pty_worker_crash_is_reaped_and_fresh_connection_recovers(native_rig):
    link = native_rig.new_link()
    link.initialize()
    pid = link.proxy._process.pid
    link.proxy._process.kill()
    link.proxy._process.wait(timeout=2)
    with pytest.raises(RuntimeError):
        link.read_pressures()
    assert_reaped(link)
    fresh = native_rig.new_link()
    fresh.initialize()
    assert fresh.proxy._process.pid != pid and math.isfinite(fresh.read_pressures().pressures[0])
    assert_reaped(fresh)


def test_actual_native_pty_healthy_close_releases_port_for_reconnect(native_rig):
    first = native_rig.new_link()
    first.initialize()
    assert_reaped(first)
    second = native_rig.new_link()
    second.initialize()
    assert math.isfinite(second.read_pressures().pressures[0])
    assert_reaped(second)


def test_actual_native_pty_exclusive_lock_rejects_second_worker_without_disturbing_owner(native_rig):
    owner = native_rig.new_link()
    owner.initialize()
    other = native_rig.new_link()
    with pytest.raises(OSError, match='opening USB port'):
        other.initialize()
    assert native_rig.serial.writes.count(b'AYT\r') == 1
    assert math.isfinite(owner.read_pressures().pressures[0])
    assert_reaped(other)
    assert math.isfinite(owner.read_pressures().pressures[0])


def run_harness(rig, *, stop_after_sample=True, simulation=False, settings=None):
    """Run the actual GUI helper-thread body without constructing unrelated Qt docks."""
    path = PLUGIN / 'tpg366_plugin.py'
    tree = ast.parse(path.read_text())
    controller = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'PressureController')
    body = next(node for node in controller.body if isinstance(node, ast.FunctionDef) and node.name == '_run')
    loads = []

    def load(part):
        loads.append(part)
        return adapter if part == '_native_link' else SimpleNamespace(NativeWorkerProxy=rig.worker_class)

    transient = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_transient_read_error')
    namespace = {'__file__': str(path), '_protocol': protocol, '_load_protocol': load, 'Path': Path, 'time': time,
                 '_READ_FAILURE_LIMIT': 3, '_RESYNC_PAUSE_S': .02,
                 '_TRANSIENT_FRAME_ERRORS': ('Expected six status/pressure pairs', 'Unknown gauge status', 'Invalid pressure')}
    exec(compile(ast.Module(body=[transient, body], type_ignores=[]), str(path), 'exec'), namespace)
    stop, events = threading.Event(), []

    def emit(event):
        events.append(event)
        if stop_after_sample and event[1] == 'sample':
            stop.set()

    host = SimpleNamespace(update=SimpleNamespace(emit=emit), interval_s=.02, _generation=1, _native_link=None,
                           controllerParent=SimpleNamespace(pluginManager=SimpleNamespace(
                               Settings=settings if settings is not None else SimpleNamespace(dataPath=rig.data_path))))
    thread = threading.Thread(target=namespace['_run'], args=(host, 1, stop, rig.serial.name, 9600, simulation),
                              name='TPG366 actual GUI helper')
    harness = SimpleNamespace(host=host, thread=thread, stop=stop, events=events, loads=loads)
    if hasattr(rig, 'harnesses'):
        rig.harnesses.append(harness)
    thread.start()
    return harness


def join_harness(harness):
    harness.thread.join(3)
    stalled = harness.thread.is_alive()
    if stalled:
        harness.stop.set()
        if harness.host._native_link is not None:
            harness.host._native_link.cancel()
        harness.thread.join(2)
    assert not stalled, 'GUI helper did not finish within its bounded stop deadline'
    assert not harness.thread.is_alive()
    assert harness.events[-1][1] == 'finished'
    assert harness.events[-1][2][0] is None and not harness.events[-1][2][2]
    assert harness.host._native_link is None


def test_actual_gui_helper_resync_records_gap_then_fresh_sample_and_reaps(native_rig):
    with native_rig.serial.lock:
        native_rig.serial.naks[b'PRX\r'] = 3
    harness = run_harness(native_rig)
    join_harness(harness)
    kinds = [kind for _generation, kind, _payload in harness.events]
    assert kinds.count('ready') == 1 and kinds.count('missed') == 1 and kinds.count('sample') == 1
    assert kinds.index('missed') < kinds.index('resynchronized') < kinds.index('recovered') < kinds.index('sample')
    assert 'error' not in kinds
    assert native_rig.serial.writes.count(b'AYT\r') == 2 and native_rig.serial.writes.count(b'PRX\r') == 4
    assert all(proxy._process.poll() is not None and all(not thread.is_alive() for thread in proxy._threads)
               for proxy in native_rig.proxies)


def test_actual_gui_helper_unit_change_during_resync_stops_before_reading_new_unit(native_rig):
    with native_rig.serial.lock:
        native_rig.serial.naks[b'PRX\r'] = 3
        native_rig.serial.change_unit_on_nak = True
    harness = run_harness(native_rig)
    join_harness(harness)
    kinds = [kind for _generation, kind, _payload in harness.events]
    assert kinds.count('missed') == 1 and 'sample' not in kinds and 'error' in kinds
    assert 'Pressure unit changed during resynchronization' in harness.events[-1][2][1]
    assert native_rig.serial.writes.count(b'PRX\r') == 3


@pytest.mark.parametrize('phase', ['ack', 'data'])
def test_actual_gui_helper_stop_during_serial_read_joins_and_reaps_without_sample(native_rig, phase):
    with native_rig.serial.lock:
        if phase == 'ack':
            native_rig.serial.hold_ack = b'PRX\r'
        else:
            native_rig.serial.hold_data = 'PRX'
    harness = run_harness(native_rig, stop_after_sample=False)
    assert native_rig.serial.started.wait(2)
    started = time.monotonic()
    link = harness.host._native_link
    harness.stop.set()
    link.cancel()
    join_harness(harness)
    assert time.monotonic() - started < 1.5
    assert not any(kind in ('sample', 'error') for _generation, kind, _payload in harness.events)
    assert_reaped(link)
    assert native_rig.serial.writes.count(b'PRX\r') == 1


def test_simulation_does_not_load_native_worker_or_need_data_path():
    rig = SimpleNamespace(data_path=None, serial=SimpleNamespace(name='SIMULATION'), worker_class=None)
    harness = run_harness(rig, simulation=True, settings=SimpleNamespace())
    join_harness(harness)
    assert not harness.loads
    assert [kind for _generation, kind, _payload in harness.events] == ['ready', 'sample', 'finished']


@pytest.mark.parametrize('settings', [SimpleNamespace(), SimpleNamespace(dataPath=None),
                                     SimpleNamespace(dataPath=''), SimpleNamespace(dataPath=' \t')])
def test_missing_explorer_data_path_fails_without_worker_or_plugin_log_fallback(settings):
    opened = []
    rig = SimpleNamespace(data_path=None, serial=SimpleNamespace(name='unused'),
                          worker_class=lambda *args, **kwargs: opened.append((args, kwargs)))
    harness = run_harness(rig, settings=settings)
    join_harness(harness)
    assert not opened
    assert [kind for _generation, kind, _payload in harness.events] == ['error', 'finished']
    assert 'dataPath' in harness.events[-1][2][1]
