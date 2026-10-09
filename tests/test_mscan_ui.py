"""Real Explorer Scan/Channel/Parameter + PSU controller under Qt, isolated process."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

from test_mscan import module, rig  # noqa: F401 -- fixtures only; native tests never run the Python reference.

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('outcome', ['error', 'raised-error', 'transport-error', 'stopped', 'raised-stopped', 'completed'])
def test_native_terminal_status_logs_once_and_closes_worker(module, monkeypatch, tmp_path, outcome):
    from threading import Event

    events, errors, loaded = [], [], []
    message = 'Native worker transport deadline expired.' if outcome == 'transport-error' else 'PSU_A_CH0: source changed or was stopped.'
    status = 'error' if outcome == 'transport-error' else outcome.removeprefix('raised-')
    validation = dict(status=status, error=message if status != 'completed' else '',
                      point_status=['not acquired'])
    scan = NS(_plan=object(), _cancel=Event(), _native_worker=None, _gui=lambda action, **kwargs: action(),
              _validation=dict(status='running', error='', point_status=['not acquired']),
              pluginManager=NS(Settings=NS(dataPath=tmp_path)),
              _bridge=NS(status=NS(emit=lambda value: events.append(('status', value)))),
              signalComm=NS(updateRecordingSignal=NS(emit=lambda value: events.append(('recording', value))),
                            scanUpdateSignal=NS(emit=lambda value: events.append(('update', value)))),
              print=lambda text, flag: errors.append((text, flag)))
    scan._queue_completion = lambda action: scan._gui(action, cancel=False)

    def forbidden(*args, **kwargs):
        raise AssertionError('The native entrypoint must not use the Python reference or hardware directly.')
    scan._run_scan_python_reference = forbidden
    worker = NS(call_method=forbidden, close=lambda **kwargs: events.append(('close', kwargs)))

    def create_worker(plugin_dir, family, config, *, log_dir):
        assert plugin_dir == ROOT / 'mscan' and family == 'mscan' and config == {}
        assert log_dir == tmp_path / 'logs' / 'mscan'
        return worker

    class TerminalAdapter:
        def __init__(self, owner, call, *, emit_completion):
            assert owner is scan and call is worker.call_method
            assert emit_completion is False
        def run(self):
            assert scan._native_worker is worker
            if outcome == 'raised-error':
                raise module.ScanError(message)
            if outcome == 'transport-error':
                raise TimeoutError(message)
            if outcome == 'raised-stopped':
                raise module.ScanStopped(message)
            # The native adapter returns terminal errors without raising and
            # replaces validation when applying its exported result.
            scan._validation = validation

    def load_native(part):
        loaded.append(part)
        return {'_native_worker': NS(NativeWorkerProxy=create_worker),
                '_native_scan': NS(NativeScanAdapter=TerminalAdapter)}[part]
    monkeypatch.setattr(module, '_load_native_runtime', load_native)
    module.MScan.runScan(scan, lambda: True)

    assert loaded == ['_native_worker', '_native_scan']
    assert scan._validation['status'] == status
    assert scan._validation['point_status'] == [status]
    expected_errors = [(f'Scan aborted: {message}', module.PRINT.ERROR)] if status == 'error' else []
    assert errors == expected_errors
    assert events == [('close', {'grace_s': 0}),
                      ('status', status.capitalize() + (': ' + message if status != 'completed' else '')),
                      ('recording', False), ('update', True)]
    assert scan._native_worker is None


@pytest.mark.parametrize('emit_completion', [None, False], ids=['standalone-default', 'entrypoint-owned'])
@pytest.mark.parametrize('transport_error', [False, True], ids=['completed', 'transport-error'])
def test_native_adapter_completion_owner_preserves_point_updates(module, monkeypatch, emit_completion, transport_error):
    from threading import Event
    import numpy as np

    notifications = []
    scan = NS(_cancel=Event(), _plan={'metadata': {}},
              _validation=dict(status='running', error='', point_status=['not acquired'] * 2,
                               rail_v=np.full((2, 1), np.nan), rail_i=np.full((2, 1), np.nan),
                               window_start=np.full(2, np.nan), window_end=np.full(2, np.nan),
                               samples=np.zeros((2, 1), dtype=int), finite_samples=np.zeros((2, 1), dtype=int)),
              outputChannels=[NS(recordingData=np.full(2, np.nan))],
              _gui=lambda action, **kwargs: action(),
              signalComm=NS(updateRecordingSignal=NS(emit=lambda value: notifications.append(('recording', value))),
                            scanUpdateSignal=NS(emit=lambda value: notifications.append(('update', value)))))
    failure = TimeoutError('Native worker transport deadline expired.')
    def unavailable(*args):
        raise failure
    kwargs = {} if emit_completion is None else {'emit_completion': emit_completion}
    adapter = module._load_native_runtime('_native_scan').NativeScanAdapter(scan, unavailable, **kwargs)
    adapter._bind()
    adapter._apply_updates(dict(state='acquiring', status='running', updates=[
        dict(index=0, mean=13., rail_v=[10.], rail_i=[.1], window_start=1., window_end=2.,
             samples=2, finite_samples=2, status='acquired')]))
    assert notifications == [('update', False)]
    notifications.clear()

    def start():
        if transport_error:
            raise failure
        adapter._running.set()
    def export_result():
        if transport_error:
            raise failure
        return dict(data=[13., 15.], metadata={'native': True},
                    validation=dict(status='completed', error='', point_status=['acquired'] * 2))
    monkeypatch.setattr(adapter, 'start', start)
    monkeypatch.setattr(adapter, 'step', adapter._running.clear)
    monkeypatch.setattr(adapter, 'export_result', export_result)
    if transport_error:
        with pytest.raises(TimeoutError, match='Native worker transport deadline expired'):
            adapter.run()
        assert scan._validation['status'] == 'error'
        assert scan._validation['error'] == str(failure)
        assert scan._validation['point_status'] == ['acquired', 'error']
        assert scan.outputChannels[0].recordingData[0] == 13.
        assert np.isnan(scan.outputChannels[0].recordingData[1])
    else:
        adapter.run()
        assert scan._validation['status'] == 'completed'
        assert scan._plan['metadata'] == {'native': True}
        assert scan.outputChannels[0].recordingData.tolist() == [13., 15.]
    expected = [('recording', False), ('update', True)] if emit_completion is None else []
    assert notifications == expected
    assert not adapter._running.is_set()


@pytest.mark.parametrize(('native_confirmation', 'native_revision', 'python_confirmation'),
                         [(10., 1, None), (None, 1, 10.), (99., 0, 10.)],
                         ids=['sync-native', 'keep-confirmed-on-none', 'ignore-stale-revision'])
def test_native_prewrite_rejects_hardware_change_after_point_update(rig, native_confirmation, native_revision,
                                                                 python_confirmation):
    scan = rig.scan
    def forbidden(*args, **kwargs):
        raise AssertionError('This native-adapter test must not run the Python reference.')
    scan.runScan = forbidden
    scan._command([10., 10.])
    psu = rig.psus[0]
    psu.controller._manual_apply_worker_running = False
    for rail in scan._plan['rails']:
        c = rail['channel']
        c._value = c.monitor = c.voltage_setpoint_readback = 10.
        rail['confirmed_vset'] = python_confirmation
    channel = psu.channels[0]
    revision = channel.voltage_request_revision
    adapter = rig.module._load_native_runtime('_native_scan').NativeScanAdapter(scan, forbidden, emit_completion=False)
    adapter._bind()
    action = dict(session=7, action_id=2, expected_revisions=[1, 1], restore=False,
                  index=1, targets=[20., 20.], latest_mono=None, offset=None)
    adapter._status = dict(session=7, state='await_command', status='running', error='', action=action,
                           revisions=[native_revision] * 2, confirmed_vset=[native_confirmation] * 2,
                           updates=[dict(index=0, mean=10., rail_v=[10., 10.], rail_i=[.002, .002],
                                         window_start=1., window_end=2., samples=2, finite_samples=2, status='acquired')])
    published = []
    def change_hardware_after_point(done):
        assert done is False and scan.outputChannels[0].recordingData[0] == 10.
        channel._value = channel.monitor = channel.voltage_setpoint_readback = 10.001
        published.append(done)
    scan.signalComm.scanUpdateSignal.emit = change_hardware_after_point
    adapter._apply_updates(adapter._status)
    with pytest.raises(rig.module.ScanError, match='PSU setpoint changed outside the scan'):
        adapter._command_on_gui(action)
    assert published == [False]
    assert psu.writes == [(0, 10.), (1, 10.)]  # No next amplitude or restoration write.
    assert rig.psus[1].writes == []
    assert psu.isOn()
    assert channel.value == channel.voltage_setpoint_readback == 10.001
    assert channel.voltage_request_revision == revision == 1
    assert [r['request_revision'] for r in scan._plan['rails']] == [1, 1]
    assert [r['confirmed_vset'] for r in scan._plan['rails']] == [10., 10.]


def test_native_late_updates_cannot_publish_into_replaced_plan(module):
    from threading import Event

    validation = dict(status='running', error='', point_status=['not acquired'])
    notifications = []
    scan = NS(_plan=object(), _cancel=Event(), _validation=validation, _gui=lambda action, **kwargs: action(),
              signalComm=NS(scanUpdateSignal=NS(emit=notifications.append)))
    adapter = module._load_native_runtime('_native_scan').NativeScanAdapter(scan, lambda *args: None)
    adapter._bind()
    scan._plan = object()
    with pytest.raises(RuntimeError, match='plan was replaced'):
        adapter._apply_updates(dict(state='finished', status='completed', error='', updates=[]))
    assert scan._validation is validation
    assert validation == dict(status='running', error='', point_status=['not acquired'])
    assert not notifications


@pytest.mark.parametrize('replacement', ['plan', 'cancel'])
@pytest.mark.parametrize('transport_error', [False, True], ids=['completed', 'transport-error'])
def test_native_late_finish_cannot_overwrite_replacement(module, monkeypatch, replacement, transport_error):
    from threading import Event
    import numpy as np

    notifications = []
    initial_plan = {'metadata': {'initial': True}}
    scan = NS(_plan=initial_plan, _cancel=Event(), scan_status='Original running',
              _validation=dict(status='running', error='', point_status=['not acquired']),
              outputChannels=[NS(recordingData=np.full(1, np.nan))],
              signalComm=NS(updateRecordingSignal=NS(emit=lambda value: notifications.append(('recording', value))),
                            scanUpdateSignal=NS(emit=lambda value: notifications.append(('update', value)))))
    new_plan = {'metadata': {'replacement': True}}
    new_validation = dict(status='running', error='', point_status=['not acquired'])
    new_data = np.asarray([222.])
    queued = []
    def gui(action, **kwargs):
        assert kwargs == {'cancel': False}
        queued.append(action.__name__)
        if replacement == 'plan':
            scan._plan = new_plan
        else:
            scan._cancel = Event()
            initial_plan['metadata'] = {'replacement': True}
        scan._validation = new_validation
        scan.outputChannels = [NS(recordingData=new_data)]
        scan.scan_status = 'Replacement running'
        return action()
    scan._gui = gui
    failure = TimeoutError('Original worker transport deadline expired.')
    def unavailable(*args):
        raise failure
    adapter = module._load_native_runtime('_native_scan').NativeScanAdapter(scan, unavailable)
    adapter._bind()
    monkeypatch.setattr(adapter, 'start', adapter._running.set)
    monkeypatch.setattr(adapter, 'step', adapter._running.clear)
    def export_result():
        if transport_error:
            raise failure
        return dict(data=[99.], metadata={'original_result': True},
                    validation=dict(status='completed', error='', point_status=['acquired']))
    monkeypatch.setattr(adapter, 'export_result', export_result)
    if transport_error:
        with pytest.raises(TimeoutError, match='Original worker transport deadline expired'):
            adapter.run()
    else:
        with pytest.raises(RuntimeError, match='replaced'):
            adapter.run()
    assert queued == ['finish']
    assert scan._validation is new_validation
    assert new_validation == dict(status='running', error='', point_status=['not acquired'])
    assert scan.outputChannels[0].recordingData is new_data and new_data.tolist() == [222.]
    assert scan._plan['metadata'] == {'replacement': True}
    assert scan.scan_status == 'Replacement running'
    assert not notifications


@pytest.mark.parametrize('replacement', ['plan', 'cancel', 'worker', 'all'])
@pytest.mark.parametrize('transport_error', [False, True], ids=['completed', 'transport-error'])
def test_native_old_finally_only_retires_its_worker(module, monkeypatch, tmp_path, replacement, transport_error):
    from threading import Event

    events, errors = [], []
    original_plan, original_cancel = object(), Event()
    validation = dict(status='running', error='', point_status=['not acquired'])
    new_validation = dict(status='running', error='', point_status=['not acquired'])
    replacement_plan, replacement_cancel = object(), Event()
    def forbidden(*args, **kwargs):
        raise AssertionError('Never close the replacement worker or run the Python reference.')
    new_worker = NS(close=forbidden)
    old_worker = NS(closed=False, call_method=forbidden)
    def close(**kwargs):
        assert kwargs == {'grace_s': 0}
        old_worker.closed = True
        events.append(('close', 'original'))
    old_worker.close = close
    scan = NS(_plan=original_plan, _cancel=original_cancel, _native_worker=None, _validation=validation,
              pluginManager=NS(Settings=NS(dataPath=tmp_path)), _run_scan_python_reference=forbidden,
              _bridge=NS(status=NS(emit=lambda value: events.append(('status', value)))),
              signalComm=NS(updateRecordingSignal=NS(emit=lambda value: events.append(('recording', value))),
                            scanUpdateSignal=NS(emit=lambda value: events.append(('update', value)))),
              print=lambda text, flag: errors.append((text, flag)))
    publications = []
    def gui(action, **kwargs):
        assert kwargs == {'cancel': False}
        if old_worker.closed:
            # Replace while the final callback is queued, not before its GUI guard.
            publications.append(action.__name__)
            if replacement in ('plan', 'all'):
                scan._plan = replacement_plan
            if replacement in ('cancel', 'all'):
                scan._cancel = replacement_cancel
            if replacement in ('worker', 'all'):
                scan._native_worker = new_worker
            scan._validation = new_validation
        return action()
    scan._gui = gui
    scan._queue_completion = lambda action: scan._gui(action, cancel=False)
    class Adapter:
        def __init__(self, owner, rpc, *, emit_completion):
            assert owner is scan and rpc is old_worker.call_method and emit_completion is False
        def run(self):
            if transport_error:
                raise TimeoutError('Original worker expired.')
            validation['status'] = 'completed'
    runtimes = {'_native_worker': NS(NativeWorkerProxy=lambda *args, **kwargs: old_worker),
                '_native_scan': NS(NativeScanAdapter=Adapter)}
    monkeypatch.setattr(module, '_load_native_runtime', runtimes.__getitem__)
    module.MScan.runScan(scan, lambda: True)
    assert publications == ['finish']
    assert old_worker.closed and events == [('close', 'original')]
    assert not errors
    assert scan._validation is new_validation
    assert new_validation == dict(status='running', error='', point_status=['not acquired'])
    assert scan._native_worker is (new_worker if replacement in ('worker', 'all') else None)
    assert scan._plan is (replacement_plan if replacement in ('plan', 'all') else original_plan)
    assert scan._cancel is (replacement_cancel if replacement in ('cancel', 'all') else original_cancel)


@pytest.mark.parametrize('replacement', ['plan', 'cancel'])
@pytest.mark.parametrize('dispatch', ['bind', 'snapshot', 'command', 'updates'])
def test_native_adapter_rejects_replaced_origin_before_dispatch(module, replacement, dispatch):
    from threading import Event

    notifications = []
    original_plan, original_cancel = {'metadata': {'original': True}}, Event()
    validation = dict(status='running', error='', point_status=['not acquired'])
    scan = NS(_plan=original_plan, _cancel=original_cancel, _validation=validation,
              _gui=lambda action, **kwargs: action(),
              signalComm=NS(scanUpdateSignal=NS(emit=notifications.append)))
    def forbidden(*args):
        raise AssertionError('A replaced session must not read hardware, execute RPCs or publish data.')
    adapter = module._load_native_runtime('_native_scan').NativeScanAdapter(scan, forbidden)
    if dispatch != 'bind':
        adapter._bind()
    replacement_plan, replacement_cancel = object(), Event()
    def replace():
        if replacement == 'plan':
            scan._plan = replacement_plan
        else:
            scan._cancel = replacement_cancel
    def gui(action, **kwargs):
        replace()  # Replacement occurs after queueing, before channel access.
        return action()
    scan._gui = gui
    if dispatch == 'bind':
        replace()
    callbacks = {'bind': adapter._bind, 'snapshot': adapter.snapshot,
                 'command': lambda: scan._gui(lambda: adapter._command_on_gui({})),
                 'updates': lambda: adapter._apply_updates({})}
    with pytest.raises(RuntimeError, match='replaced'):
        callbacks[dispatch]()
    assert scan._validation is validation and validation['status'] == 'running'
    assert original_plan == {'metadata': {'original': True}}
    assert not notifications
    adapter.cancel()
    assert original_cancel.is_set() and not replacement_cancel.is_set()
    assert adapter._plan is (None if dispatch == 'bind' else original_plan)


@pytest.mark.parametrize('replacement', ['plan', 'cancel', 'worker', 'all'])
def test_native_worker_startup_cannot_claim_replaced_session(module, monkeypatch, tmp_path, replacement):
    from threading import Event

    events, errors = [], []
    original_plan, original_cancel = object(), Event()
    replacement_plan, replacement_cancel = object(), Event()
    new_validation = dict(status='running', error='', point_status=['not acquired'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Do not adopt the replacement session, close its worker or fall back to Python.')
    new_worker = NS(close=forbidden)
    worker = NS(call_method=forbidden, close=lambda **kwargs: events.append(('close', kwargs)))
    scan = NS(_plan=original_plan, _cancel=original_cancel, _native_worker=None,
              _validation=dict(status='running', error='', point_status=['not acquired']),
              pluginManager=NS(Settings=NS(dataPath=tmp_path)), _run_scan_python_reference=forbidden,
              _gui=lambda action, **kwargs: action(),
              _bridge=NS(status=NS(emit=lambda value: events.append(('status', value)))),
              signalComm=NS(updateRecordingSignal=NS(emit=lambda value: events.append(('recording', value))),
                            scanUpdateSignal=NS(emit=lambda value: events.append(('update', value)))),
              print=lambda text, flag: errors.append((text, flag)))
    scan._queue_completion = lambda action: scan._gui(action, cancel=False)
    def startup(*args, **kwargs):
        if replacement in ('plan', 'all'):
            scan._plan = replacement_plan
        if replacement in ('cancel', 'all'):
            scan._cancel = replacement_cancel
        if replacement in ('worker', 'all'):
            scan._native_worker = new_worker
        scan._validation = new_validation
        return worker
    loaded = []
    def runtime(name):
        loaded.append(name)
        assert name == '_native_worker', 'A replaced startup must not construct or bind an adapter.'
        return NS(NativeWorkerProxy=startup)
    monkeypatch.setattr(module, '_load_native_runtime', runtime)
    module.MScan.runScan(scan, lambda: True)
    assert loaded == ['_native_worker']
    assert events == [('close', {'grace_s': 0})] and not errors
    assert scan._validation is new_validation
    assert new_validation == dict(status='running', error='', point_status=['not acquired'])
    assert scan._native_worker is (new_worker if replacement in ('worker', 'all') else None)
    assert scan._plan is (replacement_plan if replacement in ('plan', 'all') else original_plan)
    assert scan._cancel is (replacement_cancel if replacement in ('cancel', 'all') else original_cancel)
    assert not replacement_cancel.is_set()


def test_native_cancel_during_startup_completes_only_after_close(module, monkeypatch, tmp_path):
    from threading import Event

    events, closed = [], []
    scan = NS(_plan=object(), _cancel=Event(), _native_worker=None,
              _validation=dict(status='running', error='', point_status=['not acquired']),
              pluginManager=NS(Settings=NS(dataPath=tmp_path)), _gui=lambda action, **kwargs: action())
    scan._queue_completion = lambda action: scan._gui(action, cancel=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('A stopped startup must not run an adapter or the Python reference.')
    def emit(kind, value):
        assert closed and scan._native_worker is None
        events.append((kind, value))
    scan._bridge = NS(status=NS(emit=lambda value: emit('status', value)))
    scan.signalComm = NS(updateRecordingSignal=NS(emit=lambda value: emit('recording', value)),
                         scanUpdateSignal=NS(emit=lambda value: emit('update', value)))
    scan.print = forbidden
    worker = NS(call_method=forbidden, close=lambda **kwargs: closed.append(kwargs))
    def startup(*args, **kwargs):
        scan._cancel.set()
        return worker
    def runtime(name):
        assert name == '_native_worker'
        return NS(NativeWorkerProxy=startup)
    monkeypatch.setattr(module, '_load_native_runtime', runtime)
    module.MScan.runScan(scan, lambda: True)
    assert closed == [{'grace_s': 0}]
    assert events == [('status', 'Stopped: Scan stopped before native start.'),
                      ('recording', False), ('update', True)]
    assert scan._validation['point_status'] == ['stopped']


def test_native_transport_exception_survives_unavailable_gui(module, monkeypatch):
    from threading import Event

    failure = TimeoutError('Original native transport deadline expired.')
    validation = dict(status='running', error='', point_status=['not acquired'])
    scan = NS(_plan={}, _cancel=Event(), _validation=validation)
    def unavailable(*args):
        raise failure
    def gui(action, **kwargs):
        raise RuntimeError('Qt response timed out before publication.')
    scan._gui = gui
    adapter = module._load_native_runtime('_native_scan').NativeScanAdapter(scan, unavailable)
    adapter._bind()
    monkeypatch.setattr(adapter, 'start', unavailable)
    with pytest.raises(TimeoutError) as caught:
        adapter.run()
    assert caught.value is failure
    assert scan._validation is validation and validation['status'] == 'running'
    assert not adapter._running.is_set()


@pytest.mark.parametrize('transport_error', [False, True], ids=['completed', 'transport-error'])
def test_native_failed_close_never_announces_reaped_or_done(module, monkeypatch, tmp_path, transport_error):
    from threading import Event

    events, publications = [], []
    failure = TimeoutError('Native worker reap was not confirmed.')
    scan = NS(_plan=object(), _cancel=Event(), _native_worker=None,
              _validation=dict(status='running', error='', point_status=['not acquired']),
              pluginManager=NS(Settings=NS(dataPath=tmp_path)))
    def forbidden(*args, **kwargs):
        raise AssertionError('Failed close must not log a final outcome, emit done or use the reference.')
    scan.print = forbidden
    scan._queue_completion = forbidden
    scan._bridge = NS(status=NS(emit=forbidden))
    scan.signalComm = NS(updateRecordingSignal=NS(emit=forbidden), scanUpdateSignal=NS(emit=forbidden))
    def gui(action, **kwargs):
        publications.append(action.__name__)
        return action()
    scan._gui = gui
    def close(**kwargs):
        assert kwargs == {'grace_s': 0}
        events.append('close-failed')
        raise failure
    worker = NS(call_method=forbidden, close=close)
    class Adapter:
        def __init__(self, owner, rpc, *, emit_completion):
            assert owner is scan and rpc is worker.call_method and emit_completion is False
        def run(self):
            if transport_error:
                raise TimeoutError('Original transport expired.')
            scan._validation['status'] = 'completed'
    runtimes = {'_native_worker': NS(NativeWorkerProxy=lambda *args, **kwargs: worker),
                '_native_scan': NS(NativeScanAdapter=Adapter)}
    monkeypatch.setattr(module, '_load_native_runtime', runtimes.__getitem__)
    with pytest.raises(TimeoutError) as caught:
        module.MScan.runScan(scan, lambda: True)
    assert caught.value is failure
    assert publications == ['claim'] and events == ['close-failed']
    assert scan._native_worker is worker


@pytest.mark.parametrize('replacement', ['current', 'plan', 'cancel', 'worker', 'all'])
def test_native_delayed_completion_only_publishes_current_session(module, monkeypatch, tmp_path, replacement):
    from threading import Event

    events, queued = [], []
    original_plan, original_cancel = object(), Event()
    validation = dict(status='running', error='', point_status=['not acquired'])
    new_validation = dict(status='running', error='', point_status=['not acquired'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Do not close a replacement worker, log an error or use the reference.')
    worker = NS(closed=False, call_method=forbidden)
    def close(**kwargs):
        assert kwargs == {'grace_s': 0}
        worker.closed = True
        events.append(('close', 'original'))
    worker.close = close
    new_worker = NS(close=forbidden)
    scan = NS(_plan=original_plan, _cancel=original_cancel, _native_worker=None, _validation=validation,
              pluginManager=NS(Settings=NS(dataPath=tmp_path)), print=forbidden,
              _run_scan_python_reference=forbidden, _gui=lambda action, **kwargs: action())
    scan._queue_completion = lambda action: module.MScan._queue_completion(scan, action)
    def emit(kind, value):
        assert worker.closed and scan._native_worker is None
        events.append((kind, value))
    scan._bridge = NS(thread=lambda: object(), request=NS(emit=queued.append),
                      status=NS(emit=lambda value: emit('status', value)))
    scan.signalComm = NS(updateRecordingSignal=NS(emit=lambda value: emit('recording', value)),
                         scanUpdateSignal=NS(emit=lambda value: emit('update', value)))
    class Adapter:
        def __init__(self, owner, rpc, *, emit_completion):
            assert owner is scan and rpc is worker.call_method and emit_completion is False
        def run(self):
            validation['status'] = 'completed'
    runtimes = {'_native_worker': NS(NativeWorkerProxy=lambda *args, **kwargs: worker),
                '_native_scan': NS(NativeScanAdapter=Adapter)}
    monkeypatch.setattr(module, '_load_native_runtime', runtimes.__getitem__)
    module.MScan.runScan(scan, lambda: True)
    assert events == [('close', 'original')]
    assert worker.closed and scan._native_worker is worker
    assert len(queued) == 1
    call = queued[0]
    assert call.action.__name__ == 'finish' and call.cancel is None and not call.done.is_set()
    if replacement in ('plan', 'all'):
        scan._plan = object()
    if replacement in ('cancel', 'all'):
        scan._cancel = Event()
    if replacement in ('worker', 'all'):
        scan._native_worker = new_worker
    if replacement != 'current':
        scan._validation = new_validation
    # The GUI resumes beyond the normal 5s command deadline. This pure GUI
    # completion remains queued; its ownership guard runs only on delivery.
    monkeypatch.setattr(module, 'time', NS(monotonic=lambda: 10.))
    assert not call.expired.is_set()
    module._GuiBridge.execute(scan._bridge, call)
    assert call.done.is_set() and call.error is None
    if replacement == 'current':
        assert events == [('close', 'original'), ('status', 'Completed'), ('recording', False), ('update', True)]
        assert validation['point_status'] == ['completed'] and scan._native_worker is None
    else:
        assert events == [('close', 'original')]
        assert scan._validation is new_validation
        assert new_validation == dict(status='running', error='', point_status=['not acquired'])
        assert scan._native_worker is (new_worker if replacement in ('worker', 'all') else None)


def test_native_command_dispatch_still_expires_without_late_write(module, monkeypatch):
    from threading import Event

    queued, writes = [], []
    scan = NS(_cancel=Event(), _bridge=NS(thread=lambda: object(), request=NS(emit=queued.append)))
    clock = iter([0., 6.])
    monkeypatch.setattr(module, 'time', NS(monotonic=lambda: next(clock)))
    with pytest.raises(module.ScanError, match='Qt response timed out'):
        module.MScan._gui(scan, lambda: writes.append(70.))
    assert len(queued) == 1 and queued[0].expired.is_set()
    module._GuiBridge.execute(scan._bridge, queued[0])
    assert queued[0].done.is_set() and isinstance(queued[0].error, module.ScanStopped)
    assert not writes


@pytest.mark.parametrize(('scenario', 'scale'), [
    ('complete', '1'), ('stop', '1'), ('off', '1'), ('invalid', '1'), ('interface', '1'),
    ('interface', '1.5'), ('legacy-ini', '1'), ('legacy-hdf', '1'), ('native-start', '1'),
    ('readiness', '1'), ('readiness', '1.5'), ('local-stop', '1'),
    ('limits', '1'), ('limits', '1.5'), ('current-limit', '1'), ('foldback', '1'), ('auto-save', '1'), ('dock', '1'),
    ('edge-delays', '1'), ('edge-delays', '1.5'), ('edge-change', '1'),
    ('windows-settings', '1'), ('windows-save', '1'), ('windows-bom', '1'),
    ('continuous', '1'), ('continuous', '1.5'), ('continuous-stop', '1'),
    ('continuous-ilim', '1'), ('continuous-deadline', '1'), ('display-close', '1'),
    ('cadence', '1'), ('cadence', '1.5'),
    ('setpoint-stepped', '1'), ('setpoint-continuous', '1'),
    ('setpoint-channel', '1'), ('setpoint-panel', '1'), ('setpoint-roundtrip', '1'), ('setpoint-hardware', '1'),
    ('setpoint-display', '1'), ('setpoint-display', '1.5'),
    ('dmmr-interval', '1'), ('dmmr-interval', '1.5'),
    ('rate-legacy-ini', '1'), ('rate-legacy-hdf', '1'),
])
def test_mscan_in_real_explorer_and_qt(tmp_path, scenario, scale):
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONUNBUFFERED='1',
               PYTHONFAULTHANDLER='1', QT_SCALE_FACTOR=scale)
    result = subprocess.run([env.get('ESIBD_QT_PYTHON', sys.executable), str(Path(__file__)),
                             scenario, str(tmp_path)], env=env, text=True, capture_output=True, timeout=45)
    if result.returncode == 77:
        pytest.skip(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


def run(scenario, target, app_holder=None):
    host = os.environ.get('ESIBD_EXPLORER_SOURCE')
    if host:
        sys.path.insert(0, host)
    # Only unrelated desktop automation is stubbed; every Explorer/Qt class is real.
    sys.modules['pyautogui'] = ModuleType('pyautogui')
    try:
        from PyQt6.QtCore import QTimer, QThread, QObject, Qt, QRect, QSignalBlocker
        from PyQt6.QtTest import QTest
        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QApplication, QWidget, QHBoxLayout, QVBoxLayout, QTreeWidget, QLabel
        from esibd import core, plugins
        import h5py
        import numpy as np
    except ImportError as exc:
        print(f'Full Explorer/Qt dependencies unavailable: {exc}. Set ESIBD_QT_PYTHON and optionally ESIBD_EXPLORER_SOURCE.')
        return 77
    from threading import Lock, Event
    import configparser
    import time
    from psu_fakes import StatefulPSU

    app = QApplication([])
    if app_holder is not None:
        app_holder.append(app)
    app.setStyleSheet('QWidget { background: #20232a; color: #eeeeee; }'
                     'QHeaderView::section { background: #41464f; color: #eeeeee; }'
                     'QAbstractItemView { alternate-background-color: #30343c; }')
    errors, actions = [], []
    def log(**kw):
        print('LOG', kw)
        if kw.get('flag') == core.PRINT.ERROR:
            errors.append(kw.get('message'))
    class Manager(NS):
        def __getattr__(self, name):
            return getattr(plugins, name, None)
    manager = Manager(plugins=[], loading=True, closing=False, testing=False, logger=NS(print=log),
        connectAllSources=lambda: None, reconnectSource=lambda *a: None)
    manager.getPluginsByClass = lambda cls: [p for p in manager.plugins if isinstance(p, cls)]
    manager.getPluginsByType = lambda kind: [p for p in manager.plugins if getattr(p, 'pluginType', None) == kind]
    dm = manager.DeviceManager = plugins.DeviceManager.__new__(plugins.DeviceManager)
    QObject.__init__(dm)
    dm.pluginManager = manager
    dm.updateStaticPlot = lambda: None
    exports = []
    dm.exportConfiguration = lambda file: exports.append(file)
    manager.Settings = NS(loading=True, configPath=target / 'config', dataPath=target, dependencyPath=ROOT / 'mscan',
        sourceCodePath=ROOT / 'mscan/mscan_plugin.py', sessionPath=target,
        measurementNumber=1, getFullSessionPath=lambda: target, saveSettings=lambda **kw: None)
    manager.Settings.configPath.mkdir(exist_ok=True)
    manager.Explorer = NS(root=None, populateTree=lambda: None, activeFileFullPath=None)
    manager.Text = NS(setText=lambda *a, **kw: None)

    def load(slug, file):
        # Match PluginManager.loadPluginsFromPath, including no sys.modules entry.
        name = Path(file).stem
        assert name not in sys.modules
        module = core.dynamicImport(name, ROOT / slug / file)
        assert module is not None
        assert name not in sys.modules
        return module
    psum = load('psu_d' if scenario.startswith('setpoint-') else 'psu_a', 'psu_plugin.py')
    mscan = load('mscan', 'mscan_plugin.py')
    amxm = load('amx_a', 'amx_plugin.py')
    from test_amx_outputs import config79_snapshot
    window = QWidget()
    outer = QHBoxLayout(window)
    sources_widget, scan_widget, plot_widget = QWidget(), QWidget(), QWidget()
    sources_layout, scan_layout, plot_layout = QVBoxLayout(sources_widget), QVBoxLayout(scan_widget), QVBoxLayout(plot_widget)
    outer.addWidget(sources_widget, 1)
    outer.addWidget(scan_widget, 1)
    outer.addWidget(plot_widget, 2)

    def device_fields(parent, name, unit, inout, monitors):
        QObject.__init__(parent)
        parent.name, parent._loading, parent.channels = name, 0, []
        parent.loading = True
        parent.pluginManager = manager
        parent.interval = 10
        parent.updating = False
        parent.unit, parent.inout = unit, inout
        parent.useDisplays, parent.useMonitors = True, monitors
        parent.useBackgrounds, parent.logY = False, False
        parent.convertDataDisplay, parent.liveDisplay = None, None
        parent.recordingAction = NS(state=True)
        parent.recording = True
        parent.maxDataPoints = 100000
        parent.time = core.DynamicNp(max_size=100000, dtype=np.float64)
        parent.isOn = lambda: True
        parent.makeCoreIcon = lambda *a, **kw: QIcon()
        parent.print = lambda message, flag=core.PRINT.MESSAGE: log(message=message, flag=flag)
        parent._schedule_delayed_refresh = lambda *a: None
        parent.tree = QTreeWidget()
        sources_layout.addWidget(QLabel(name))
        sources_layout.addWidget(parent.tree)
        manager.plugins.append(parent)

    psu = psum.PSUDevice.__new__(psum.PSUDevice)
    device_fields(psu, 'PSU_A', 'V', core.INOUT.IN, True)
    psu.startup_timeout_s, psu.poll_timeout_s, psu.interlock_monitoring = 1., .1, True
    psu.controller = controller = psum.PSUController(psu)
    controller.lock, controller.initialized = Lock(), True
    controller.print = psu.print
    for i in (0, 1):
        c = psum.PSUChannel(psu, psu.tree)
        psu.tree.setColumnCount(len(c.parameters))
        psu.tree.addTopLevelItem(c)
        psu.channels.append(c)
        c.initGUI({'Name': f'PSU_A_CH{i}', 'CH': str(i)})
    class Hardware(StatefulPSU):
        def __init__(self):
            super().__init__()
            self.voltage_limits = {0: 350., 1: 200.}
            self.forced_current = None
            self.drop_voltage = 0.
        def get_channel_voltage_limits(self, channel, **kw):
            return self.voltages[channel], self.voltage_limits[channel]
        def set_channel_voltage(self, channel, voltage, **kw):
            super().set_channel_voltage(channel, voltage, **kw)
            if scenario.startswith('setpoint-') and scenario != 'setpoint-display':
                # A representable setpoint, not a changed user request. This
                # 3 mV example is within the PSU driver's existing verification
                # tolerance; it is not a claim about the physical DAC resolution.
                self.voltages[channel] = round(voltage / .003) * .003
        def collect_housekeeping(self, **kw):
            return dict(main_state={'name': 'ST_ON'}, device_enabled=self.enabled, output_enabled=self.outputs,
                psu_state={'psu_enabled_actual': self.enabled, 'interlock_active': True,
                           'interlock_out_disabled': False, 'interlock_bnc_disabled': False},
                device_state={'flags': []}, channels=[dict(channel=i, enabled=self.outputs[i],
                    voltage={'measured_v': self.get_channel_measurements(i)[0], 'set_v': self.voltages[i],
                             'limit_v': self.get_channel_voltage_limits(i)[1]},
                    current={'measured_a': self.get_channel_measurements(i)[1], 'set_a': self.currents[i],
                             'limit_a': self.get_channel_current_limits(i)[1]},
                    full_range={'enabled': self.ranges[i], 'supported': True}) for i in (0, 1)])
        def get_channel_measurements(self, channel, **kw):
            current = self.currents[channel] / 2 if self.forced_current is None else self.forced_current
            return self.voltages[channel] - .1 - self.drop_voltage, current, 15.
    hw = controller.device = Hardware()
    hw.enabled, hw.outputs, hw.interlocks = True, (True, True), (True, True)
    hw.voltages, hw.currents = {0: 100., 1: 110.}, {0: .001, 1: .001}
    psu.loading = False
    controller._update_state()

    dmmr = load('dmmr', 'dmmr_plugin.py')
    detector_device = dmmr.DMMRDevice.__new__(dmmr.DMMRDevice)
    device_fields(detector_device, 'DMMR', 'A', core.INOUT.IN, True)
    detector_device.dependencyPath, detector_device.sourceCodePath = ROOT / 'dmmr', ROOT / 'dmmr/dmmr_plugin.py'
    detector_device.controller = dmmr_controller = dmmr.DMMRController(detector_device)
    dmmr_controller.initialized = dmmr_controller.acquiring = True
    dmmr_controller.print = detector_device.print
    detector = dmmr.DMMRChannel(detector_device, detector_device.tree)
    detector_device.tree.setColumnCount(len(detector.parameters))
    detector_device.tree.addTopLevelItem(detector)
    detector_device.channels.append(detector)
    detector.initGUI({'Name': 'DMMR_M03', 'Module': '3'})
    detector.values = core.DynamicNp(max_size=100000)
    detector_device.loading = False
    if scenario.startswith(('dmmr-', 'rate-')):
        # The real global Setting, including the native intervalChanged event,
        # persistence and the same property wrapper used by Explorer. No DLL.
        interval_tree = core.TreeWidget()
        sources_layout.addWidget(interval_tree)
        global_mgr = plugins.SettingsManager(parentPlugin=manager.Settings, pluginManager=manager,
            tree=interval_tree, defaultFile=target / 'global.ini', dependencyPath=ROOT / 'dmmr',
            sourceCodePath=ROOT / 'dmmr/dmmr_plugin.py')
        interval_key = f'DMMR/{detector_device.INTERVAL}'
        definitions = detector_device.getDefaultSettings()
        detector_device.maxStorage = 50
        detector_device.defaultChannel = detector
        for key in (interval_key, f'DMMR/{detector_device.MAXDATAPOINTS}'):
            definition = definitions[key]
            global_mgr.defaultSettings[key] = definition
            global_mgr.addSetting({**definition, core.Parameter.NAME: key, core.Parameter.TREE: interval_tree})
            setattr(dmmr.DMMRDevice, definition[core.Parameter.ATTR], plugins.makeSettingWrapper(key, global_mgr))
        global_interval = global_mgr.settings[interval_key]
        manager.Settings.settings = global_mgr.settings
        assert detector_device.interval == 1000
        assert global_interval.spin.minimum() == 100 and global_interval.spin.maximum() == 10000
    # Real AMX register decoding, not a hand-written "Periodic"/timing row.
    amx_snapshot = config79_snapshot()
    if scenario.startswith('edge-'):
        for switch in amx_snapshot['switches']:
            switch['trigger_delay'] = dict(rise=3, fall=7)
    amx = NS(name='AMX_A', psu_ch01='PSU_A', psu_ch23='None', isOn=lambda: True,
        controller=NS(initialized=True, initializing=False, transitioning=False, device=object(),
            output_rows=amxm._amx_output_rows(amx_snapshot)))
    manager.plugins.append(amx)
    scan = mscan.MScan(pluginManager=manager, dependencyPath=ROOT / 'mscan', sourceCodePath=ROOT / 'mscan/mscan_plugin.py')
    manager.plugins.append(scan)
    scan.addContentWidget = scan_layout.addWidget
    if scenario.startswith('windows-'):
        import configparser
        native_read = configparser.ConfigParser.read
        def windows_read(parser, filenames, encoding=None):
            # Simulate the Windows locale only when the caller omits encoding.
            return native_read(parser, filenames, encoding=encoding or 'cp1252')
        configparser.ConfigParser.read = windows_read
        settings_file = manager.Settings.configPath / scan.configINI
        unicode_note = 'Réglages “courant ionique” : 50%'
        seed = ('[Notes]\nValue = ' + unicode_note + '\n\n'
                '[DMMR module]\nValue = Module 3 — DMMR_M03\n\n'
                '[AMX]\nValue = AMX_A\n\n'
                '[Amplitude from (V)]\nValue = 63.5\n\n'
                '[External metadata]\nValue = ' + unicode_note + '\n')
        if scenario != 'windows-save':
            settings_file.write_text(seed, encoding='utf-8-sig' if scenario == 'windows-bom' else 'utf-8')
        calls_before_settings = list(hw.calls)
    scan.initGUI()
    native_completions, native_saves = [], []
    if scenario == 'auto-save':
        native = mscan._load_native_runtime('_native_worker')
        original_proxy, created = native.NativeWorkerProxy, []
        class ObservedWorker(original_proxy):
            def __init__(self, *args, **kwargs):
                assert 'command' not in kwargs  # Keep the real manifest-verified packaged executable.
                super().__init__(*args, **kwargs)
                created.append(self)
        native.NativeWorkerProxy = ObservedWorker
        def observe_completion(done):
            if done:
                worker = created[-1]
                native_completions.append((worker._process.poll() is not None,
                                           worker._closed, scan._native_worker is None))
        scan.signalComm.scanUpdateSignal.connect(observe_completion)
        scan.signalComm.saveScanCompleteSignal.connect(lambda: native_saves.append(scan._native_worker is None))
    scan_layout.insertWidget(0, scan.titleBar)
    if scenario.startswith('windows-'):
        if scenario == 'windows-save':
            # Saving an existing UTF-8 file also used the broken locale reader.
            with settings_file.open('a', encoding='utf-8') as f:
                f.write('\n[External metadata]\nValue = ' + unicode_note + '\n')
            scan.settingsMgr.saveSettings(useDefaultFile=True)
        else:
            assert scan.notes == unicode_note
            assert scan.start == 63.5
            assert scan.detector_module == 'Module 3 — DMMR_M03'
            assert scan._detector() is detector
        manager.loading = manager.Settings.loading = False
        scan.amx_name, scan.amx_outputs = 'AMX_A', 'CH2-CH3'
        scan.start, scan.stop, scan.step = 72.5, 122.5, 2.5
        scan.settling_s, scan.integration_s = .4, 2.5
        scan.detector_module = 'Module 3 — DMMR_M03'
        scan.settingsMgr.saveSettings(useDefaultFile=True)
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.read(settings_file, encoding='utf-8-sig')
        assert parsed['External metadata']['Value'] == unicode_note
        assert parsed['DMMR module']['Value'] == 'Module 3 — DMMR_M03'
        assert parsed['Info']['Plugin'] == 'MScan Settings'
        scan.settingsMgr.loadSettings(useDefaultFile=True)
        assert (scan.start, scan.stop, scan.step) == (72.5, 122.5, 2.5)
        assert (scan.settling_s, scan.integration_s) == (.4, 2.5)
        assert scan.amx_name == 'AMX_A' and scan.amx_outputs == 'CH2-CH3'
        assert scan._detector() is detector
        exported = target / 'Réglages “copie”.INI'
        exported.write_bytes(settings_file.read_bytes())
        imported_bytes = exported.read_bytes()
        scan.loadSettings(file=exported)
        assert exported.read_bytes() == imported_bytes
        assert scan._detector() is detector
        # Invalid UTF-8 must not discard settings or overwrite the source file.
        broken = target / 'broken.ini'
        broken.write_bytes(b'[Notes]\nValue = bad \xff\n')
        settings_before = dict(scan.settingsMgr.settings)
        with pytest.raises(UnicodeDecodeError):
            scan.settingsMgr.loadSettings(file=broken)
        assert scan.settingsMgr.settings == settings_before
        assert not scan.settingsMgr.loading
        assert broken.read_bytes() == b'[Notes]\nValue = bad \xff\n'
        from unittest.mock import patch
        settings_bytes = settings_file.read_bytes()
        with patch.object(mscan.QFileDialog, 'getOpenFileName', return_value=('', '')):
            scan.settingsMgr.loadSettings()
        with patch.object(mscan.QFileDialog, 'getSaveFileName', return_value=('', '')):
            scan.settingsMgr.saveSettings()
        assert scan.settingsMgr.settings == settings_before
        assert settings_file.read_bytes() == settings_bytes
        assert not scan.settingsMgr.loading
        assert hw.calls == calls_before_settings
        assert not errors, errors
        sources_widget.hide()
        plot_widget.hide()
        window.resize(580, 720)
        window.show()
        app.processEvents()
        window.grab().save(str(target / f'{scenario}.png'))
        scan.closeGUI()
        print('WINDOWS_SETTINGS_OK', scenario)
        return 0
    assert scan.titleBarLabel.text() == 'msScan — AMX/PSU'
    assert scan.settingsTree.headerItem().text(0) == 'msScan'
    assert scan.channelTree is None
    assert not {'Display', 'Notes', 'Wait', 'Wait long', 'Average', 'Large step', 'Channel'}.intersection(scan.settingsMgr.settings)
    start_button = scan.titleBar.widgetForAction(scan.recordingAction)
    assert start_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    assert start_button.text() == 'Start scan'
    scan.statusAction = NS(setVisible=lambda *a: None)
    manager.loading = False
    manager.Settings.loading = False
    scan.loading = True
    scan.amx_name, scan.amx_outputs = 'AMX_A', 'CH0-CH1'
    scan.start, scan.stop, scan.step = 60., 80., 10.
    scan.sweep_rate = 25.
    scan.settling_s = .025
    scan.integration_s, scan.settle_timeout, scan.voltage_tolerance = .055, 1., 1.
    scan.loading = False
    scan._refresh_interface()
    measured = scan.settingsMgr.settings[scan.DETECTOR]
    assert measured.text(0) == 'DMMR module'
    assert measured.fixedItems
    assert measured.value == scan.NO_SIGNAL
    assert 'Module 3 — DMMR_M03' in measured.items
    assert not any('PSU' in item for item in measured.items)
    assert 'Detector' not in measured.items
    # Actual keyboard selection, without adding/editing an item by hand.
    measured.combo.setFocus()
    QTest.keyClick(measured.combo, Qt.Key.Key_Home)
    QTest.keyClick(measured.combo, Qt.Key.Key_Down)
    assert measured.value == 'Module 3 — DMMR_M03'
    pairs = scan.settingsMgr.settings[scan.OUTPUTS]
    assert pairs.items == [scan.NO_PAIR, 'CH0-CH1', 'CH2-CH3']
    assert 'CH0-CH3' not in pairs.items
    for p in (psu, detector_device):
        p.tree.setHeaderLabels([a.name for a in p.channels[0].parameters])
        for i, parameter in enumerate(p.channels[0].parameters):
            p.tree.setColumnHidden(i, parameter.name not in ('Name', 'Value', 'Monitor'))
        p.tree.setMaximumHeight(130)
    # Full native figure and data-copy action, but not the surrounding dock manager.
    scan.display = scan.Display(scan=scan, pluginManager=manager)
    scan.display.addContentWidget = plot_layout.addWidget
    scan.display.initGUI()
    scan.display.displayComboBox = core.CompactComboBox()
    scan.display.displayComboBox.currentIndexChanged.connect(scan.updateDisplayChannel)
    plot_layout.addWidget(scan.display.displayComboBox)
    scan.display.raiseDock = lambda *a, **kw: None
    # Keep the already embedded display when opening files, but preserve the
    # host's close path. Skipping Display.closeGUI leaves its shared toolbar
    # actions alive during unrelated widget destruction (Qt removeAction crash).
    scan.toggleDisplay = lambda visible: None if visible else scan.display.closeGUI()
    scan.updateFile = lambda: None
    scan.displayActive = lambda: True
    scan.file = target / 'native.mscan.h5'
    window.resize(1420, 660)
    window.show()
    app.processEvents()

    before_calls = list(hw.calls)
    if scenario.startswith('rate-legacy-'):
        scan.scan_mode, scan.time_step_s, scan.sweep_rate = scan.CONTINUOUS, .6, 10. / .6
        legacy = target / ('speed-old.ini' if scenario.endswith('ini') else 'speed-old.h5')
        scan.saveSettings(file=legacy)
        if legacy.suffix == '.ini':
            import configparser
            saved = configparser.ConfigParser(interpolation=None)
            saved.read(legacy, encoding='utf-8')
            saved.remove_section(scan.RATE)
            saved['Requested sweep rate'] = {'Value': '99999 V/s', 'Default': 'Unavailable'}
            saved[scan.DMMR_INTERVAL]['Value'] = '100'
            with legacy.open('w', encoding='utf-8') as handle:
                saved.write(handle)
        else:
            with h5py.File(legacy, 'a') as handle:
                group = handle['MScan/Settings']
                del group[scan.RATE]
                group.create_group('Requested sweep rate').attrs['Value'] = '99999 V/s'
                group[scan.DMMR_INTERVAL].attrs['Value'] = 100
        before = legacy.read_bytes()
        global_interval.value = 500
        scan.sweep_rate, scan.time_step_s, scan.step = 7., .2, 3.
        scan.loadSettings(file=legacy)
        assert scan.sweep_rate == pytest.approx(10. / .6, rel=1e-13)
        assert scan.time_step_s == .6 and scan.step == 10.
        assert scan._scan_steps() == pytest.approx([60., 70., 80.], rel=1e-13)
        rate_spin = scan.settingsMgr.settings[scan.RATE].spin
        rate_spin.interpretText()  # Start consumes pending text, not a rounded old display.
        assert scan._scan_steps() == pytest.approx([60., 70., 80.], rel=1e-13)
        current = target / 'speed-new.ini'
        scan.saveSettings(file=current)
        scan.loadSettings(file=current)
        assert scan._scan_steps() == pytest.approx([60., 70., 80.], rel=1e-13)
        assert scan.settingsMgr.settings[scan.RATE].fullName == 'Sweep rate'  # V/s is only a header
        assert detector_device.interval == global_interval.value == 500
        assert scan.settingsMgr.settings[scan.DMMR_INTERVAL].value == 500
        assert before == legacy.read_bytes() and hw.calls == before_calls
        scan.closeGUI()
        window.close()
        return 0
    if scenario == 'cadence':
        # Real channels: M03 delivers 10 results/s, M05 only 5 results/s even
        # though both share the same recorder clock. Equal currents are new data.
        def history(values):
            h = core.DynamicNp(max_size=100000, dtype=np.float64)
            for value in values:
                h.add(value)
            return h
        detector_device.loading = True
        slow = dmmr.DMMRChannel(detector_device, detector_device.tree)
        detector_device.tree.addTopLevelItem(slow)
        detector_device.channels.append(slow)
        slow.initGUI({'Name': 'DMMR_M05', 'Module': '5'})
        detector_device.loading = False
        now = time.time()
        detector_device.time = history([now - .4, now - .3, now - .2, now - .1, now])
        detector.values = history([1e-12] * 5)
        slow.values = history([1e-12, np.nan, 1e-12, np.nan, 1e-12])
        scan.scan_mode = scan.CONTINUOUS
        scan.time_step_s = .1
        scan._refresh_interface()
        assert scan.scan_status == 'Ready to scan', scan.scan_status
        assert start_button.isEnabled()
        assert 'Time step ≥ 0.1 s' in scan.settingsMgr.settings[scan.CADENCE].value
        measured.combo.setCurrentText('Module 5 — DMMR_M05')
        scan._refresh_interface()
        assert 'Time step must be at least 0.2 s' in scan.scan_status, scan.scan_status
        assert not start_button.isEnabled()
        assert scan.time_step_s == .1  # no silently clamped or changed settings
        assert not scan.initScan()
        assert hw.calls == before_calls
        sources_widget.hide()
        plot_widget.hide()
        window.resize(620, 740)
        app.processEvents()
        window.grab().save(str(target / 'cadence-blocked.png'))
        scan.time_step_s = .2
        scan._refresh_interface()
        assert scan.scan_status == 'Ready to scan', scan.scan_status
        assert start_button.isEnabled()
        app.processEvents()
        window.grab().save(str(target / 'cadence-ready.png'))
        # The configured interval also matters, even with previous fast data.
        detector_device.interval = 500
        scan._refresh_interface()
        assert 'after DMMR interval change' in scan.scan_status, scan.scan_status
        assert not start_button.isEnabled()
        detector_device.interval = 10
        slow.values = history([np.nan] * 5)
        scan._refresh_interface()
        assert 'two valid recorded DMMR samples' in scan.scan_status, scan.scan_status
        assert not start_button.isEnabled()
        assert hw.calls == before_calls
        scan.closeGUI()
        window.close()
        return 0
    if scenario == 'display-close':
        from PyQt6 import sip
        # Exercise both native destruction orders, without scans, fake actions,
        # timing sleeps or suppressing faults in Qt. Each display owns its UI.
        for cycle in range(40):
            display = scan.Display(scan=scan, pluginManager=manager)
            display.initGUI()
            toolbar = display.titleBar
            navigation_actions = list(display.navToolBar.actions())
            assert any(action in toolbar.actions() for action in navigation_actions)
            assert any(action.text() == 'Data to Clipboard.' for action in toolbar.actions())
            display.closeGUI()
            assert not toolbar.actions()  # clipboard too, not only Matplotlib navigation
            for widget in ((display, toolbar) if cycle % 2 else (toolbar, display)):
                sip.delete(widget)
            app.processEvents()
        assert hw.calls == before_calls and not errors
        scan_toolbar = scan.titleBar
        scan.closeGUI()
        assert not scan_toolbar.actions()
        window.close()
        return 0
    if scenario == 'readiness':
        # Idle checks use cached data only: no initScan, data reset, signal
        # selection, cancellation token replacement or hardware command.
        plan, validation, cancel = scan._plan, scan._validation, scan._cancel
        inputs, outputs = list(scan.inputChannels), list(scan.outputChannels)
        def check_status(fragment):
            scan._refresh_interface()
            assert fragment in scan.scan_status, scan.scan_status
            assert scan._plan is plan and scan._validation is validation and scan._cancel is cancel
            assert scan.inputChannels == inputs and scan.outputChannels == outputs
            assert hw.calls == before_calls
            assert start_button.text() == 'Start scan'
        check_status('Ready to scan')
        detector_device.recording = False
        check_status('DMMR_M03: start detector recording')
        sources_widget.hide()
        plot_widget.hide()
        window.resize(400, 600)
        QTest.qWait(40)
        window.grab().save(str(target / 'mscan-detector-not-recording.png'))
        # An attempted start still uses the native action and leaves both rails alone.
        QTest.mouseClick(start_button, Qt.MouseButton.LeftButton)
        assert not scan.recording and scan.finished and scan.runThread is None
        assert hw.calls == before_calls and 'recording' in scan.scan_status
        plan, validation = scan._plan, scan._validation
        inputs, outputs = list(scan.inputChannels), list(scan.outputChannels)
        detector_device.recording = True
        detector_device.controller.acquiring = False
        check_status('DMMR_M03: start detector acquisition')
        detector_device.controller.acquiring = True
        detector_device.controller.initialized = False
        check_status('DMMR_M03: initialize the detector')
        detector_device.controller.initialized = True
        detector_device.loading = True
        detector.enabled = False
        check_status('DMMR_M03: enable the detector channel')
        detector.enabled = True
        detector_device.loading = False
        amx.isOn = lambda: False
        check_status('AMX_A: turn the AMX ON')
        amx.isOn = lambda: True
        amx.controller.transitioning = True
        check_status('wait for the AMX transition')
        amx.controller.transitioning = False
        for row in amx.controller.output_rows:
            row.pop('waveform', None)
            row.pop('timing', None)
        check_status('AMX_A CH0: AMX edge-delay readback unavailable')
        assert scan.settingsMgr.settings[scan.FREQUENCY].value == '500 kHz'
        amx_snapshot['switches'][0]['trigger_config'] = 3
        amx.controller.output_rows = amxm._amx_output_rows(amx_snapshot)
        check_status('AMX_A CH0: External')
        assert 'DIN0' in scan.scan_status
        amx_snapshot['switches'][0]['trigger_config'] = 10
        amx.controller.output_rows = amxm._amx_output_rows(amx_snapshot)
        check_status('Ready to scan')
        amx.psu_ch01 = 'None'
        check_status('associate a PSU')
        amx.psu_ch01 = 'PSU_A'
        psu.isOn = lambda: False
        check_status('PSU_A_CH0: turn the PSU ON')
        psu.isOn = lambda: True
        psu.channels[1].monitor = np.nan
        check_status('PSU_A_CH1: no valid PSU voltage readback')
        with controller.lock:
            controller._update_state()
        before_calls = list(hw.calls)
        # Inject a limit for preflight without coercing the live Vset widget
        # (a real edit of that limit would legitimately command a lower voltage).
        with QSignalBlocker(psu.channels[1].getParameterByName(core.Channel.MAX).getWidget()):
            psu.channels[1].max = 65.
            check_status('exceeds allowed')
            psu.channels[1].max = 10000.
        other_scan = plugins.Scan.__new__(plugins.Scan)
        other_scan.name, other_scan._finished = 'Other scan', False
        manager.plugins.append(other_scan)
        check_status('Finish Other scan')
        manager.plugins.remove(other_scan)
        check_status('Ready to scan')
        # Readiness recovers on the actual timer, without clicking Start again.
        detector_device.recording = False
        QTest.qWait(650)
        assert 'start detector recording' in scan.scan_status
        detector_device.recording = True
        QTest.qWait(650)
        assert 'Ready to scan' in scan.scan_status
        QTest.qWait(30)
        assert start_button.isVisible() and start_button.width() >= start_button.sizeHint().width()
        window.grab().save(str(target / 'mscan-ready.png'))
        sources_widget.show()
        plot_widget.show()
        window.resize(1420, 660)
    if scenario == 'limits':
        assert scan.settingsMgr.settings[scan.AMPLITUDE_LIMITS].value == '0 – 200'
        tooltip = scan.settingsMgr.settings[scan.AMPLITUDE_LIMITS].label.toolTip()
        assert 'PSU_A CH0: channel 0–10000 V; PSU-reported maximum 350 V' in tooltip
        assert 'PSU_A CH1: channel 0–10000 V; PSU-reported maximum 200 V' in tooltip
        hw.voltage_limits[1] = 350.
        controller._update_state()
        scan._refresh_interface()
        assert scan.settingsMgr.settings[scan.AMPLITUDE_LIMITS].value == '0 – 350'
        hw.voltage_limits[1] = 200.
        controller._update_state()
        scan._refresh_interface()
        final = scan.settingsMgr.settings[scan.FINAL_VOLTAGES].value
        assert 'CH0: +100' in final and 'CH1: −110' in final, final
        assert '1 mA (max 1000 mA)' in scan.settingsMgr.settings[scan.CURRENT_LIMITS].value
        def blocked_start(message):
            scan._refresh_interface()
            assert message in scan.scan_status, scan.scan_status
            QTest.mouseClick(start_button, Qt.MouseButton.LeftButton)
            assert not scan.recording and scan.finished and scan.runThread is None
            assert message in scan.scan_status, scan.scan_status
            assert hw.calls == before_calls
        # Check both declared endpoints, including a To that step alignment skips.
        scan.stop, scan.step = 210., 80.
        blocked_start('exceeds allowed 0–200 V')
        sources_widget.hide()
        plot_widget.hide()
        window.resize(460, 760)
        QTest.qWait(40)
        window.grab().save(str(target / 'mscan-rejected-range.png'))
        scan.stop, scan.step = 80., 10.
        hw.voltage_limits[1] = 90.  # scan fits, but restoration to 110 V would not
        controller._update_state()
        blocked_start('final restoration to 110 V')
        hw.voltage_limits[1] = 200.
        hw.currents[1] = 0.
        controller._update_state()
        blocked_start('Ilim must be positive')
        hw.currents[1] = .001
        hw.forced_current = .001
        controller._update_state()
        blocked_start('reached/exceeded Ilim')
        hw.forced_current = None
        controller._update_state()
        # Missing/expired capabilities must not default to 10000 V or zero.
        controller._limits_readback = None
        controller.updateValues()
        blocked_start('hardware voltage limit unavailable')
        assert scan.settingsMgr.settings[scan.AMPLITUDE_LIMITS].value == 'Unavailable'
        QTest.qWait(20)
        for key in (scan.FINAL_VOLTAGES, scan.CURRENT_LIMITS):
            label = scan.settingsMgr.settings[key].label
            assert label.height() <= label.fontMetrics().height() + 4, (key, label.text(), label.size())
        controller._update_state()
        # Real Parameter precision must accept unrounded hardware observations.
        hw.voltages[0] = 100.123456789
        controller._update_state()
        scan._refresh_interface()
        assert 'Ready to scan' in scan.scan_status, (scan.scan_status, psu.channels[0].value,
            psu.channels[0].voltage_setpoint_readback)
        hw.voltages[0] = 100.
        controller._update_state()
        scan._refresh_interface()
        QTest.qWait(40)
        window.grab().save(str(target / 'mscan-allowed-range.png'))
        sources_widget.show()
        plot_widget.show()
        # Resize callbacks must never request another item layout on the same
        # stack as QTreeWidget.updateEditorGeometries (native stack overflow).
        from PyQt6.QtGui import QResizeEvent
        label = scan.settingsMgr.settings[scan.STATUS].label
        old_size, new_size = label.size(), label.size()
        new_size.setWidth(old_size.width() + 1)
        fitted, original_fit = [], scan._fit_readonly_label
        def observed_fit(widget):
            fitted.append(widget)
            original_fit(widget)
        scan._fit_readonly_label = observed_fit
        scan.eventFilter(label, QResizeEvent(new_size, old_size))
        assert not fitted  # only queue a coalesced layout for the next event turn
        QTest.qWait(20)
        assert label in fitted
        scan._fit_readonly_label = original_fit
        for _ in range(40):
            window.resize(1420, 660)
            QTest.qWait(2)
            window.resize(620, 540)
            QTest.qWait(2)
        window.resize(1420, 660)
    if scenario.startswith('legacy-'):
        legacy = target / ('old.ini' if scenario == 'legacy-ini' else 'old.h5')
        scan.saveSettings(file=legacy)
        if scenario == 'legacy-ini':
            import configparser
            config = configparser.ConfigParser()
            config.read(legacy)
            for new, old in ((scan.START, 'From'), (scan.STOP, 'To'), (scan.STEP, 'Step'), (scan.OUTPUTS, 'AMX outputs')):
                config[old] = dict(config[new])
                del config[new]
            for key in (scan.DETECTOR, scan.SETTLING, scan.INTEGRATION):
                del config[key]
            config['AMX outputs']['Value'] = 'CH0-CH3'
            config['AMX outputs']['Default'] = 'CH0-CH3'
            config['AMX outputs']['Items'] = 'CH0-CH1,CH2-CH3,CH0-CH3'
            config['Display'] = {'Value': 'DMMR_M03', 'Items': 'Detector,DMMR_M03,MissingOldDetector'}
            config['Wait'] = {'Value': '20'}
            config['Wait long'] = {'Value': '25'}
            config['Average'] = {'Value': '55'}
            with legacy.open('w') as f:
                config.write(f)
        else:
            with h5py.File(legacy, 'a') as f:
                group = f['MScan/Settings']
                for new, old in ((scan.START, 'From'), (scan.STOP, 'To'), (scan.STEP, 'Step'), (scan.OUTPUTS, 'AMX outputs')):
                    group.move(new, old)
                for key in (scan.DETECTOR, scan.SETTLING, scan.INTEGRATION):
                    del group[key]
                group['AMX outputs'].attrs['Value'] = 'CH0-CH3'
                group['AMX outputs'].attrs['Default'] = 'CH0-CH3'
                group['AMX outputs'].attrs['Items'] = 'CH0-CH1,CH2-CH3,CH0-CH3'
                display = group.create_group('Display')
                display.attrs['Value'] = 'DMMR_M03'
                display.attrs['Items'] = 'Detector,DMMR_M03,MissingOldDetector'
                for key, value in (('Wait', 20), ('Wait long', 25), ('Average', 55)):
                    group.create_group(key).attrs['Value'] = value
        original_file = legacy.read_bytes()
        scan.loadSettings(file=legacy)
        assert legacy.read_bytes() == original_file  # importing is strictly read-only
        assert scan.settling_s == .025 and scan.integration_s == .055
        measured = scan.settingsMgr.settings[scan.DETECTOR]
        assert measured.value == 'Module 3 — DMMR_M03'
        assert measured.fixedItems and measured.text(0) == 'DMMR module'
        assert not {'Detector', 'MissingOldDetector'}.intersection(measured.items)
        assert scan.start == 60. and scan.stop == 80. and scan.step == 10.
        pairs = scan.settingsMgr.settings[scan.OUTPUTS]
        assert 'CH0-CH3' not in pairs.items and scan.amx_outputs == scan.NO_PAIR
        assert not start_button.isEnabled() and 'Select one AMX pair' in scan.scan_status
        pairs.combo.setCurrentText('CH0-CH1')  # explicit physical choice, never silent migration
        assert len(scan.outputChannels) == 1
        assert scan.outputChannels[0].sourceChannel is detector
        assert scan.settingsMgr.settings[scan.START].text(0) == 'Amplitude from (V)'
    if scenario == 'interface':
        # Placeholder, removed channel, duplicate name, and recording stopped:
        # each must block, with no PSU command and no fallback detector.
        measured.value = scan.NO_SIGNAL
        assert not scan.initScan() and 'Select a DMMR module' in scan.scan_status
        measured.value = 'Module 3 — DMMR_M03'
        detector_device.channels.remove(detector)
        scan._refresh_interface()
        assert measured.value == 'Module 3 — DMMR_M03'
        assert 'missing/ambiguous' in measured.combo.itemData(measured.combo.currentIndex(), Qt.ItemDataRole.ToolTipRole)
        assert not scan.initScan() and 'unavailable' in scan.scan_status
        detector_device.channels.append(detector)
        detector_device.loading = True
        other = dmmr.DMMRChannel(detector_device, detector_device.tree)
        detector_device.tree.addTopLevelItem(other)
        other.initGUI({'Name': 'DMMR_M03', 'Module': '3'})
        detector_device.channels.append(other)
        detector_device.loading = False
        scan._refresh_interface()
        assert not scan.initScan() and 'ambiguous' in scan.scan_status
        other.name = 'DMMR_M05'
        other.module = 5
        scan._refresh_interface()
        assert 'Module 5 — DMMR_M05' in measured.items, (measured.items, other.name, scan.finished, scan.recording,
            scan.loading, scan.settingsMgr.loading, manager.loading, [c.name for c in dm.channels()])
        assert measured.value == 'Module 3 — DMMR_M03'
        # The second *available* detector deliberately has no sample history.
        # It must not be acquired just because it is a choice in the combo.
        assert len(other.values.get()) == 0
        detector_device.recording = False
        assert not scan.initScan() and 'recording' in scan.scan_status
        detector_device.recording = True
        amx.psu_ch23 = 'PSU_A'
        scan._refresh_interface()
        assert 'Also affects AMX_A CH2-CH3' in scan.settingsMgr.settings[scan.SUPPLIES].value
        amx.psu_ch23 = 'None'
        amx.controller.output_rows = None
        scan._refresh_interface()
        assert scan.settingsMgr.settings[scan.FREQUENCY].value == 'Unavailable'
        amx.controller.output_rows = amxm._amx_output_rows(amx_snapshot)
        amx.psu_ch01 = 'PSU_B'
        scan._refresh_interface()
        assert scan.settingsMgr.settings[scan.SUPPLIES].value == 'PSU_B: unavailable'
        amx.psu_ch01 = 'PSU_A'
        scan._refresh_interface()
    assert hw.calls == before_calls  # discovery, restoring settings and failed preflight never touch hardware

    def sample():
        assert QThread.currentThread() is app.thread()
        with controller.lock:
            controller._update_state()
        value = math_value(hw.voltages[0])
        if scenario == 'invalid' and hw.voltages[0] == 70.:
            value = np.nan
        with dmmr_controller._sample_lock:
            dmmr_controller.values = {3: value, 5: 2 * value}
            dmmr_controller.meas_ranges = {3: -10, 5: -10}
            dmmr_controller._sample_token = object()
        dmmr_controller.updateValues()
        for channel in detector_device.channels:
            channel.appendValue(lenT=len(detector_device.time.get()))
        detector_device.time.add(time.time())
    def math_value(v):
        return float(np.exp(-((v - 70) / 9) ** 2) * 1e-12)
    sample()
    timer = QTimer()
    timer.timeout.connect(sample)
    timer.start(8)
    if scenario == 'dmmr-interval':
        timer.stop()
        mode, rate = scan.settingsMgr.settings[scan.MODE], scan.settingsMgr.settings[scan.RATE]
        proxy = scan.settingsMgr.settings[scan.DMMR_INTERVAL]
        mode.combo.setCurrentText(scan.CONTINUOUS)
        scan.time_step_s = .4
        scan._refresh_interface()
        assert proxy.value == 1000 and proxy.spin.isEnabled()
        assert (proxy.spin.minimum(), proxy.spin.maximum()) == (100, 10000)
        assert 'ALL' in proxy.toolTip and 'hardware sampling-rate' in proxy.toolTip
        assert scan.settingsMgr.settings[scan.STEP].isHidden() and not rate.isHidden()
        assert 'Time step' in scan.scan_status and not start_button.isEnabled()
        rate.spin.setFocus()
        rate.spin.selectAll()
        QTest.keyClicks(rate.spin, '10')
        QTest.keyClick(rate.spin, Qt.Key.Key_Return)
        assert scan.sweep_rate == 10. and scan.step == 10.
        assert scan._scan_steps().tolist() == [60., 64., 68., 72., 76., 80.]
        old_history = detector.values
        old_times = detector_device.time.get().copy()
        proxy.spin.setFocus()
        proxy.spin.selectAll()
        QTest.keyClicks(proxy.spin, '100')
        scan._refresh_interface()
        assert proxy.spin.lineEdit().text() == '100'
        assert detector_device.interval == 1000  # pending input, not committed yet
        QTest.keyClick(proxy.spin, Qt.Key.Key_Return)
        assert detector_device.interval == global_interval.value == 100
        assert detector_device.interval_tolerance == 100  # real native intervalChanged
        persisted = configparser.ConfigParser()
        persisted.read(global_mgr.defaultFile, encoding='utf-8')
        assert persisted.getint('DMMR/Interval', 'Value') == 100
        assert detector.values is old_history
        assert np.array_equal(detector_device.time.get(), old_times)
        assert hw.calls == before_calls
        scan._refresh_interface()
        assert 'after DMMR interval change' in scan.scan_status, scan.scan_status
        assert not start_button.isEnabled()
        def wait_for_cadence():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                QTest.qWait(20)
                scan._refresh_interface()
                if scan.scan_status == 'Ready to scan':
                    return
            raise AssertionError((scan.scan_status, detector_device.interval, scan._dmmr_cadence_epoch,
                                  detector_device.time.get()[-6:], detector.getValues()[-6:], time.time()))
        # Simulated module/recorder uses the configured software interval, not an ADC clock.
        # Await the measured condition, not a fixed sleep that assumes timer delivery.
        timer.setInterval(detector_device.interval)
        timer.start()
        wait_for_cadence()
        sources_widget.hide()
        plot_widget.hide()
        window.resize(500, 740)
        QTest.qWait(30)
        window.grab().save(str(target / 'rate-and-dmmr.png'))
        # A toolbar/shortcut may start without moving focus: consume the live
        # DMMR edit first, then refuse movement until its new cadence is known.
        timer.stop()
        commands_before_edit = list(hw.calls)
        proxy.spin.setFocus()
        proxy.spin.selectAll()
        QTest.keyClicks(proxy.spin, '200')
        scan.recordingAction.trigger()  # QAction activation without a focus-out
        assert detector_device.interval == global_interval.value == 200, (
            detector_device.interval, proxy.value, proxy.spin.isEnabled(), proxy.spin.specialValueText(), scan.scan_status)
        assert not scan.recording and scan.runThread is None
        assert hw.calls == commands_before_edit
        scan._refresh_interface()
        assert 'after DMMR interval change' in scan.scan_status
        proxy.value = 100
        proxy.changedEvent()
        timer.setInterval(100)
        timer.start()
        wait_for_cadence()
        QTest.mouseClick(start_button, Qt.MouseButton.LeftButton)
        thread = scan.runThread
        assert thread is not None and (not proxy.spin.isEnabled() or proxy.spin.isReadOnly())
        proxy.value = 500  # even scripted edits cannot bypass the run lock
        proxy.changedEvent()
        assert detector_device.interval == 100
        deadline = time.monotonic() + 12
        while not scan.finished and time.monotonic() < deadline:
            QTest.qWait(5)
        thread.join(.5)
        assert scan.finished and scan._validation['status'] == 'completed', scan._validation
        timer.stop()
        assert np.isfinite(scan.outputChannels[0].recordingData).all()
        assert detector_device.interval == 100  # no automatic restoration of DMMR
        with h5py.File(scan.file) as handle:
            settings = handle['MScan/Settings']
            assert settings[scan.RATE].attrs['Value'] == 10.
            assert settings[scan.DMMR_INTERVAL].attrs['Value'] == 100
            assert handle['MScan/Input Channels/Amplitude'][:].tolist() == [60., 64., 68., 72., 76., 80.]
        global_interval.value = 500
        global_interval.changedEvent()
        proxy.spin.clearFocus()
        scan._refresh_interface()
        assert proxy.value == 500
        scan.loadData(scan.file)
        assert detector_device.interval == scan.settingsMgr.settings[scan.DMMR_INTERVAL].value == 500
        assert scan.sweep_rate == 10. and scan.time_step_s == .4
        assert not errors, errors
        scan.closeGUI()
        window.close()
        return 0
    if scenario.startswith('setpoint-'):
        psu.name = amx.psu_ch01 = 'PSU_D'
        for i, channel in enumerate(psu.channels):
            channel.name = f'PSU_D_CH{i}'
        QTest.qWait(40)
        if scenario == 'setpoint-display':
            hw.voltages, hw.currents = {0: 4., 1: 4.}, {0: .064, 1: .064}
            hw.forced_current = .032  # Iget is independent of the programmed Ilim.
            sample()
            scan._refresh_interface()
            label = scan.settingsMgr.settings[scan.FINAL_VOLTAGES].label
            limits = scan.settingsMgr.settings[scan.CURRENT_LIMITS]
            expected = ['PSU_D_CH0: +4 V, Iget 32 mA', 'PSU_D_CH1: −4 V, Iget 32 mA']
            assert label.text().splitlines() == expected, label.text()
            assert 'Iget' in label.toolTip() and 'measured now' in label.toolTip() and 'not a prediction' in label.toolTip()
            assert '64 mA (max 1000 mA)' in limits.value
            def grouped_limits():
                index = scan.settingsTree.indexOfTopLevelItem
                assert index(limits) == index(scan.settingsMgr.settings[scan.AMPLITUDE_LIMITS]) + 1
            grouped_limits()
            assert scan.scan_status == 'Ready to scan', scan.scan_status
            calls = list(hw.calls)
            sources_widget.hide()
            plot_widget.hide()
            window.resize(460, 760)
            QTest.qWait(650)  # include the normal timer refresh after layout
            for key in (scan.FINAL_VOLTAGES, scan.CURRENT_LIMITS):
                value_label = scan.settingsMgr.settings[key].label
                bounds = value_label.fontMetrics().boundingRect(QRect(0, 0, value_label.width(), 10000),
                    Qt.TextFlag.TextWordWrap, value_label.text())
                assert bounds.height() <= value_label.height() <= bounds.height() + 4, (
                    key, value_label.size(), bounds, value_label.sizeHint(), value_label.minimumSizeHint(),
                    value_label.heightForWidth(value_label.width()), value_label.minimumHeight(), value_label.maximumHeight())
            window.grab().save(str(target / 'after-completion.png'))
            hw.currents[0] = .128  # Changing Ilim must not masquerade as a measured current.
            QTest.qWait(650)
            assert label.text().splitlines() == expected
            assert '128 mA (max 1000 mA)' in limits.value
            # The actual refresh timer must update Iget during a run without
            # replacing the saved return target by the moving setpoint.
            scan._plan = scan._preflight()
            scan.finished = False
            hw.voltages, hw.forced_current = {0: 60., 1: 60.}, .0075
            QTest.qWait(650)
            assert label.text().splitlines() == ['PSU_D_CH0: +4 V, Iget 7.5 mA', 'PSU_D_CH1: −4 V, Iget 7.5 mA'], label.text()
            window.grab().save(str(target / 'after-completion-running.png'))
            hw.forced_current = np.nan
            QTest.qWait(650)
            assert label.text().splitlines() == ['PSU_D_CH0: +4 V, Iget unavailable', 'PSU_D_CH1: −4 V, Iget unavailable']
            hw.forced_current = 0.
            QTest.qWait(650)
            assert 'Iget 0 mA' in label.text() and 'unavailable' not in label.text()
            scan.finished = True
            hw.voltages, hw.forced_current = {0: 4., 1: 4.}, .032
            sample()
            scan._refresh_interface()
            assert label.text().splitlines() == expected
            assert hw.calls == calls  # This display must not send any command.
            scan.saveSettings(file=target / 'current-display.ini')
            scan.loadSettings(file=target / 'current-display.ini')
            limits = scan.settingsMgr.settings[scan.CURRENT_LIMITS]
            grouped_limits()
            assert scan.settingsMgr.settings[scan.FINAL_VOLTAGES].label.text().splitlines() == expected
            timer.stop()
            scan.closeGUI()
            window.close()
            return 0
        if scenario == 'setpoint-continuous':
            scan.scan_mode, scan.time_step_s = scan.CONTINUOUS, .4
        scan._refresh_interface()
        assert scan.scan_status == 'Ready to scan', scan.scan_status
        interrupted = []
        external = scenario not in ('setpoint-stepped', 'setpoint-continuous')
        live_summaries = []
        def change_setpoint(done):
            if not done:
                scan._refresh_interface()
                text = scan.settingsMgr.settings[scan.FINAL_VOLTAGES].value
                assert 'PSU_D_CH0: +100 V, Iget ' in text and 'PSU_D_CH1: −110 V, Iget ' in text, text
                live_summaries.append(text)
            if done or not external or interrupted:
                return
            assert QThread.currentThread() is app.thread()
            interrupted.append(True)
            c = psu.channels[0]
            original, revision = float(c.value), c.voltage_request_revision
            if scenario == 'setpoint-hardware':
                hw.voltages[0] += .001
                sample()
                assert c.voltage_request_revision == revision
            elif scenario == 'setpoint-panel':
                psu._submit_manual_panel_state({'setpoints_only': True, 'voltage_values': {0: original + .001}})
                assert c.voltage_request_revision == revision + 1
            else:
                c.value = original + .001
                if scenario == 'setpoint-roundtrip':
                    c.value = original
                assert c.voltage_request_revision > revision
        scan.signalComm.scanUpdateSignal.connect(change_setpoint)
        QTest.mouseClick(start_button, Qt.MouseButton.LeftButton)
        thread = scan.runThread
        assert thread is not None
        deadline = time.monotonic() + 12
        while (not scan.finished or controller._manual_apply_worker_running) and time.monotonic() < deadline:
            QTest.qWait(5)
        thread.join(.5)
        timer.stop()
        assert scan.finished and not thread.is_alive() and not controller._manual_apply_worker_running
        result = scan._validation
        assert live_summaries and any('Iget 0.5 mA' in text for text in live_summaries), live_summaries
        print('SETPOINT_RESULT', result['status'], result['error'], hw.calls)
        assert result['status'] == ('error' if external else 'completed'), result
        if external:
            assert interrupted and 'PSU_D_CH0' in result['error'] and 'setpoint changed outside the scan' in result['error']
            assert np.isfinite(scan.outputChannels[0].recordingData[0])
            assert np.isnan(scan.outputChannels[0].recordingData[1:]).all()
            assert not any(call[0] == 'voltage' and call[2] in (70., 80., 100., 110.) for call in hw.calls), hw.calls
            assert len(errors) == 1 and 'Scan aborted' in errors[0], errors
        else:
            assert np.isfinite(scan.outputChannels[0].recordingData).all()
            assert not errors, errors
            assert hw.voltages == pytest.approx({0: 100., 1: 110.}, abs=.002)
        assert hw.outputs == (True, True) and hw.enabled
        assert not any(call[0] == 'current' for call in hw.calls)
        scan.closeGUI()
        window.close()
        return 0
    if scenario.startswith('continuous'):
        # Preflight requires a measured module cadence, not a single old value.
        QTest.qWait(40)
        # Use the real combo, Start/Stop action, controller worker, DMMR recorder,
        # plotting and automatic HDF5 saving. No generic Channel substitutes.
        mode = scan.settingsMgr.settings[scan.MODE]
        assert mode.items == [scan.STEPPED, scan.CONTINUOUS]
        assert mode.value == scan.STEPPED
        assert scan.settingsMgr.settings[scan.TIME_STEP].isHidden()
        assert not scan.settingsMgr.settings[scan.INTEGRATION].isHidden()
        mode.combo.setCurrentText(scan.CONTINUOUS)
        scan.time_step_s = .4
        scan._refresh_interface()
        assert scan.scan_mode == scan.CONTINUOUS
        assert scan.settingsMgr.settings[scan.INTEGRATION].isHidden()
        assert not scan.settingsMgr.settings[scan.TIME_STEP].isHidden()
        assert scan.settingsMgr.settings[scan.SETTLING].text(0) == 'Initial/final settling (s)'
        assert scan.settingsMgr.settings[scan.RATE].value == 25.
        assert scan.settingsMgr.settings[scan.RATE].text(0) == 'Sweep rate (V/s)'
        assert scan.settingsMgr.settings[scan.STEP].isHidden()
        assert scan._scan_steps().tolist() == [60., 70., 80.]
        assert scan.scan_status == 'Ready to scan', scan.scan_status
        scan.saveSettings(file=target / 'continuous.ini')
        broken = target / 'unknown-mode.ini'
        broken.write_text((target / 'continuous.ini').read_text(encoding='utf-8').replace(
            'value = Continuous', 'value = Unknown mode'), encoding='utf-8')
        scan.loadSettings(file=broken)
        scan._refresh_interface()
        assert scan.scan_mode == 'Unknown mode'
        assert 'Select Step by step or Continuous' in scan.scan_status
        assert not start_button.isEnabled()
        assert hw.calls == before_calls
        scan.loadSettings(file=target / 'continuous.ini')
        assert scan.scan_mode == scan.CONTINUOUS and scan.time_step_s == .4
        assert scan.settingsMgr.settings[scan.INTEGRATION].isHidden()
        window.resize(1540, 780)
        QTest.qWait(30)
        window.grab().save(str(target / 'continuous-ready.png'))
        if scenario == 'continuous-deadline':
            original = scan._command
            def late_callback(targets, latest=None, **kwargs):
                if latest is not None:
                    time.sleep(.85)  # Qt callback arrives too late: no stale command permitted.
                return original(targets, latest=latest, **kwargs)
            scan._command = late_callback
        QTest.mouseClick(start_button, Qt.MouseButton.LeftButton)
        thread = scan.runThread
        assert thread is not None and start_button.text() == 'Stop scan'
        assert not scan.settingsMgr.settings[scan.MODE].getWidget().isEnabled()
        assert scan.settingsMgr.settings[scan.TIME_STEP].spin.isReadOnly()
        interrupted = False
        deadline = time.monotonic() + 12
        while thread.is_alive() and time.monotonic() < deadline:
            if scenario in ('continuous-stop', 'continuous-ilim') and not interrupted and np.isfinite(scan.outputChannels[0].recordingData[0]):
                if scenario == 'continuous-stop':
                    QTest.mouseClick(start_button, Qt.MouseButton.LeftButton)
                else:
                    hw.forced_current = .001
                    sample()
                interrupted = True
            QTest.qWait(2)
        thread.join(.5)
        assert not thread.is_alive(), 'Continuous scan worker hung'
        deadline = time.monotonic() + 5
        while not scan.finished and time.monotonic() < deadline:
            QTest.qWait(10)
        timer.stop()
        assert scan.finished and scan.file in exports, (scan.scan_status, exports)
        validation = scan._validation
        print('CONTINUOUS_RESULT', validation['status'], validation['error'], hw.calls)
        expected = 'completed' if scenario == 'continuous' else 'stopped' if scenario == 'continuous-stop' else 'error'
        assert validation['status'] == expected, validation
        assert hw.outputs == (True, True) and hw.enabled
        assert not any(call[0] == 'current' for call in hw.calls)
        assert start_button.text() == 'Start scan'
        if scenario == 'continuous':
            assert hw.voltages == {0: 100., 1: 110.}
            assert np.isfinite(scan.outputChannels[0].recordingData).all()
            assert not errors, errors
        else:
            assert not any(call[0] == 'voltage' and call[2] in (80., 100., 110.) for call in hw.calls), hw.calls
            assert np.isnan(scan.outputChannels[0].recordingData[1:]).all()
            if scenario == 'continuous-stop':
                assert interrupted and not errors
            else:
                assert len(errors) == 1 and 'Scan aborted' in errors[0], errors
            if scenario == 'continuous-deadline':
                assert 'time step missed' in validation['error']
                assert not any(call[0] == 'voltage' and call[2] == 70. for call in hw.calls)
        with h5py.File(scan.file) as f:
            assert f['MScan/Settings/Scan mode'].attrs['Value'] == scan.CONTINUOUS
            assert f['MScan/Settings/Time step (s)'].attrs['Value'] == .4
            assert f['MScan/Validation'].attrs['status'] == expected
            raw = f['MScan/Validation/Continuous']
            times, currents = raw['detector_time'][:], raw['detector_current'][:]
            assert len(times) == len(currents) and len(times) > 0
            assert (np.diff(times) > 0).all()
            assert raw['psu_v'].shape[1] == 2
            for i, (start, end) in enumerate(zip(validation['window_start'], validation['window_end'])):
                if np.isfinite(start):
                    selected = currents[(times > start) & (times <= end)]
                    assert scan.outputChannels[0].recordingData[i] == pytest.approx(np.mean(selected), rel=1e-12)
        values = scan.outputChannels[0].recordingData.copy()
        scan.loadData(scan.file)
        assert scan.scan_mode == scan.CONTINUOUS
        assert np.array_equal(scan.outputChannels[0].recordingData, values, equal_nan=True)
        scan.plot(done=True)
        QTest.qWait(30)
        assert scan.display.axes[0].get_xlabel() == 'Commanded amplitude A (V)'
        assert scan.settingsMgr.settings[scan.MODE].getWidget().isEnabled()
        window.grab().save(str(target / 'continuous-result.png'))
        sources_widget.hide()
        plot_widget.hide()
        window.resize(440, 660)
        scan._refresh_interface()
        QTest.qWait(40)
        window.grab().save(str(target / 'continuous-compact.png'))
        scan.settingsMgr.settings[scan.MODE].combo.setCurrentText(scan.STEPPED)
        assert scan.settingsMgr.settings[scan.TIME_STEP].isHidden()
        assert scan.settingsMgr.settings[scan.RATE].isHidden()
        assert not scan.settingsMgr.settings[scan.INTEGRATION].isHidden()
        assert not scan.settingsMgr.settings[scan.STEP].isHidden()
        assert scan.settingsMgr.settings[scan.SETTLING].text(0) == scan.SETTLING
        assert scan.time_step_s == .4
        # This chosen continuous schedule is slower, not an automatic fast mode.
        assert float(scan.scantime.split()[0]) < 1.25
        # Changing the next scan's mode must not relabel the loaded data.
        scan.plot(done=True)
        assert scan.display.axes[0].get_xlabel() == 'Commanded amplitude A (V)'
        assert np.array_equal(scan.outputChannels[0].recordingData, values, equal_nan=True)
        if scenario == 'continuous':
            import configparser
            legacy = configparser.ConfigParser(interpolation=None)
            legacy.read(target / 'continuous.ini', encoding='utf-8')
            legacy.remove_section(scan.MODE)
            legacy.remove_section(scan.TIME_STEP)
            old_file = target / 'before-modes.ini'
            with old_file.open('w', encoding='utf-8') as handle:
                legacy.write(handle)
            original_bytes = old_file.read_bytes()
            scan.settingsMgr.settings[scan.MODE].combo.setCurrentText(scan.CONTINUOUS)
            scan.loadSettings(file=old_file)
            assert scan.scan_mode == scan.STEPPED and scan.time_step_s == 1.
            assert scan.settingsMgr.settings[scan.TIME_STEP].isHidden()
            assert not scan.settingsMgr.settings[scan.INTEGRATION].isHidden()
            assert old_file.read_bytes() == original_bytes
        # A native navigation action is shared with the display title bar.
        # Closing must detach those actions while both toolbars still exist.
        title_bar = scan.display.titleBar
        navigation_actions = list(scan.display.navToolBar.actions())
        assert any(action in title_bar.actions() for action in navigation_actions)
        scan.closeGUI()
        assert not any(action in title_bar.actions() for action in navigation_actions)
        window.close()
        return 0
    # The protocol drives the actual PSU Parameter/worker path; original Scan plotting remains live.
    if scenario != 'auto-save':
        scan.signalComm.scanUpdateSignal.disconnect()
        scan.signalComm.scanUpdateSignal.connect(lambda done: (scan._acquisition_info(), scan.plot(update=not done, done=done)))
    scan.notes = 'Legacy session note, not a scan control.'
    if scenario in ('native-start', 'auto-save', 'edge-delays'):
        if scenario == 'edge-delays':
            scan._refresh_interface()
            assert scan.scan_status == 'Ready to scan'
            assert scan.settingsMgr.settings[scan.FREQUENCY].value == '500 kHz'
            assert 'timing' not in amx.controller.output_rows[0]
            assert start_button.isEnabled()
            QTest.qWait(40)
            window.grab().save(str(target / 'mscan-edge-delays-ready.png'))
        scan.step = 3.
        spin = scan.settingsMgr.settings[scan.STEP].spin
        spin.setFocus()
        spin.lineEdit().selectAll()
        QTest.keyClicks(spin.lineEdit(), '10')
        assert spin.value() == 3. and spin.lineEdit().text() == '10'
        # The real start action consumes the text even without focus loss/Enter.
        QTest.mouseClick(start_button, Qt.MouseButton.LeftButton)
        assert scan.step == 10.
        thread = scan.runThread
        assert thread is not None
    else:
        scan.recording = True
        scan.initData()
        assert scan.initScan(), f'initScan failed: {scan.scan_status}'
        scan.finished = False
        from threading import Thread
        thread = Thread(target=scan.runScan, args=(lambda: scan.recording,))
        thread.start()
    assert start_button.text() == 'Stop scan' and start_button.isEnabled()
    assert 'HV stays ON' in start_button.toolTip()
    if scenario == 'readiness':
        sources_widget.hide()
        plot_widget.hide()
        window.resize(400, 600)
        QTest.qWait(40)
        window.grab().save(str(target / 'mscan-running.png'))
        # Idle availability must never replace the in-progress status or disable Stop.
        running_status = scan.scan_status
        detector_device.recording = False
        scan._refresh_interface()
        assert scan.scan_status == running_status and start_button.isEnabled()
        detector_device.recording = True
    assert [c.name for c in scan.outputChannels] == ['DMMR_M03']
    assert scan.channelTree is None and scan.inputChannelGroupItem is None
    assert 'PSU_A' in scan.settingsMgr.settings[scan.SUPPLIES].value
    assert 'CH0 = CH1 = A' in scan.settingsMgr.settings[scan.SUPPLIES].value
    assert '500 kHz' in scan.settingsMgr.settings[scan.FREQUENCY].value
    for key in (scan.DETECTOR, scan.AMX, scan.OUTPUTS):
        assert not scan.settingsMgr.settings[key].getWidget().isEnabled()
    for key in (scan.START, scan.INTEGRATION):
        assert scan.settingsMgr.settings[key].getWidget().isReadOnly()
    if scenario == 'native-start':
        before_settings = dict(scan.settingsMgr.settings)
        scan.loadSettings(useDefaultFile=True)
        assert scan.settingsMgr.settings == before_settings
        assert scan.step == 10.
    interrupted = False
    deadline = time.monotonic() + 12
    while thread.is_alive() and time.monotonic() < deadline:
        app.processEvents()
        if scenario in ('stop', 'local-stop', 'off', 'current-limit', 'foldback', 'edge-change') and not interrupted and np.isfinite(scan.outputChannels[0].recordingData[0]):
            if scenario == 'edge-change':
                amx_snapshot['switches'][0]['trigger_delay']['rise'] += 1
                amx.controller.output_rows = amxm._amx_output_rows(amx_snapshot)
            elif scenario == 'current-limit':
                hw.forced_current = .001
                sample()
            elif scenario == 'foldback':
                hw.drop_voltage = 10.
                sample()
            elif scenario == 'stop':
                scan.recording = False  # Global Explorer stop path, not just the local button.
            elif scenario == 'local-stop':
                QTest.mouseClick(start_button, Qt.MouseButton.LeftButton)
                assert scan._cancel.is_set() and not scan.recording
            else:
                controller._cancel_output_commands()
                psu.isOn = lambda: False
            interrupted = True
            actions = list(hw.calls)
        time.sleep(.001)
    thread.join(.5)
    assert not thread.is_alive(), 'Scan worker hung'
    timer.stop()
    app.processEvents()
    if scenario == 'auto-save':
        deadline = time.monotonic() + 5
        while not scan.finished and time.monotonic() < deadline:
            QTest.qWait(10)
        assert scan.finished and scan.file in exports, (scan.scan_status, exports)
        assert native_completions == [(True, True, True)], native_completions
        assert native_saves == [True], native_saves
        assert exports.count(scan.file) == 1
    print('RESULT', scan._validation, 'HW', hw.calls)
    assert start_button.text() == 'Start scan'
    assert start_button.isEnabled() == (scenario == 'auto-save')  # wait until the file has been saved
    if scenario in ('stop', 'local-stop', 'off', 'current-limit', 'foldback', 'edge-change'):
        assert interrupted
        assert scan._validation['status'] == ('stopped' if scenario in ('stop', 'local-stop') else 'error')
        if scenario == 'current-limit':
            assert 'reached/exceeded Ilim' in scan._validation['error']
            assert hw.currents == {0: .001, 1: .001}
        elif scenario == 'foldback':
            assert 'voltage' in scan._validation['error'].lower()
        elif scenario == 'edge-change':
            assert 'AMX waveform' in scan._validation['error']
        assert np.isnan(scan._validation['rail_i'][1:]).all()
        assert np.isnan(scan.outputChannels[0].recordingData[1:]).any()
        assert not any(call[0] == 'voltage' and call[2] in (100., 110.) for call in hw.calls), hw.calls
    else:
        assert scan._validation['status'] == 'completed', scan._validation
        assert hw.voltages == {0: 100., 1: 110.}, hw.voltages
        if scenario == 'invalid':
            assert np.isnan(scan.outputChannels[0].recordingData[1])
            assert scan._validation['point_status'][1] == 'invalid detector data'
        else:
            assert scan.outputChannels[0].recordingData == pytest.approx(
                [math_value(v) for v in (60, 70, 80)], rel=1e-6, abs=1e-20)
        assert (scan._validation['samples'] > 0).all()
    assert not any(call[0] in ('outputs', 'device', 'range') for call in hw.calls), hw.calls
    assert 'valid /' in scan.settingsMgr.settings[scan.SAMPLES].value
    if scenario != 'auto-save':
        scan.saveData(scan.file)
    with h5py.File(scan.file) as f:
        assert f['MScan'].attrs['notes'] == 'Legacy session note, not a scan control.'
        if scenario == 'auto-save':
            assert {'DMMR module', 'Settling time (s)', 'Measurement time (s)'} <= set(f['MScan/Settings'])
            assert not {'Notes', 'Display', 'Wait', 'Wait long', 'Average', 'Large step', 'Channel'} & set(f['MScan/Settings'])
        assert f['MScan/Input Channels/Amplitude'][:].tolist() == [60, 70, 80]
        assert np.array_equal(f['MScan/Output Channels/DMMR_M03'][:], scan.outputChannels[0].recordingData, equal_nan=True)
        assert f['MScan/Validation'].attrs['status'] == scan._validation['status']
        assert np.array_equal(f['MScan/Validation/rail_i'][:], scan._validation['rail_i'], equal_nan=True)
    scan.plot(done=True)
    QTest.qWait(30)
    status_label = scan.settingsMgr.settings[scan.STATUS].label
    status_bounds = status_label.fontMetrics().boundingRect(QRect(0, 0, status_label.width(), 10000),
        Qt.TextFlag.TextWordWrap, status_label.text())
    assert status_label.height() >= status_bounds.height(), (status_label.text(), status_label.size(), status_bounds)
    assert scan.display.ms.get_marker() == '.'  # isolated valid points must remain visible beside NaNs
    assert np.array_equal(scan.display.ms.get_ydata(), scan.outputChannels[0].recordingData, equal_nan=True)
    window.grab().save(str(target / f'mscan-{scenario}.png'))
    if scenario not in ('stop', 'local-stop', 'off', 'invalid', 'current-limit', 'foldback', 'edge-change'):
        original = scan.outputChannels[0].recordingData.copy()
        scan.finished = True
        scan.loadData(scan.file)
        assert np.array_equal(scan.outputChannels[0].recordingData, original)
        assert scan._validation['status'] == 'completed'
        if scenario == 'legacy-hdf':
            # Existing files with multiple recorded outputs still load and plot
            # even if one of their original devices is no longer installed.
            with h5py.File(scan.file, 'a') as f:
                del f['MScan'].attrs['notes']
                f.require_group('MScan/Settings/Notes').attrs['Value'] = 'Original archived comment'
                group = f['MScan/Output Channels']
                group.copy('DMMR_M03', 'Former_detector')
                group['Former_detector'][:] = original * 2
                del f['MScan/Validation/rail_i']  # older files have no PSU-current record
                for name in ('samples', 'finite_samples'):
                    group = f['MScan/Validation']
                    data = group[name][:]
                    del group[name]
                    group.create_dataset(name, data=np.column_stack((data, data)))
            scan.loadData(scan.file)
            assert scan.notes == 'Original archived comment'
            assert [c.name for c in scan.outputChannels] == ['DMMR_M03', 'Former_detector']
            assert np.isnan(scan._validation['rail_i']).all()
            scan.plot(done=True)
            scan.display.displayComboBox.setCurrentText('Former_detector')
            app.processEvents()
            assert np.array_equal(scan.display.ms.get_ydata(), original * 2), (
                scan.getOutputIndex(), scan.display.displayComboBox.currentText(),
                scan.recording, scan.loading, scan.initializing, scan.settingsMgr.loading,
                scan.display.ms.get_ydata(), scan.outputChannels[1].recordingData, original * 2)
            scan._refresh_interface()
            assert scan.displayDefault == 'DMMR_M03'
            assert len(scan.outputChannels) == 2
    if scenario == 'off':
        assert len(errors) == 1 and 'source changed or was stopped' in errors[0], errors
    elif scenario in ('current-limit', 'foldback', 'edge-change'):
        assert len(errors) == 1 and 'Scan aborted' in errors[0], errors
        assert not any(c[0] == 'current' for c in hw.calls)
        assert hw.outputs == (True, True) and hw.enabled
    else:
        assert not errors, errors
    scan.finished = True
    scan._refresh_interface()
    assert scan.scantime == '0.265 s + ramping/data delays', scan.scantime
    assert scan.settingsMgr.settings[scan.TIMEOUT].isHidden()
    scan.toggleAdvanced(advanced=True)
    assert not scan.settingsMgr.settings[scan.TIMEOUT].isHidden()
    scan.toggleAdvanced(advanced=False)
    assert scan.settingsMgr.settings[scan.TIMEOUT].isHidden()
    assert start_button.text() == 'Start scan' and start_button.isEnabled() == scan._ready
    assert scan.settingsMgr.settings[scan.DETECTOR].getWidget().isEnabled()
    if scenario in ('stop', 'local-stop', 'off'):
        outcome = scan.scan_status
        scan._refresh_interface()
        assert outcome in scan.scan_status  # readiness never erases why the last scan stopped
    if scenario == 'readiness':
        before_data = scan.outputChannels[0].recordingData.copy()
        before_calls = list(hw.calls)
        # Saving failure remains visible after idle polling resumes.
        scan.finished = False
        from threading import Thread
        worker = Thread(target=scan.saveScanParallel, args=(target / 'missing-folder' / 'cannot-save.h5',))
        worker.start()
        while worker.is_alive():
            app.processEvents()
            time.sleep(.001)
        worker.join()
        app.processEvents()
        assert scan.finished and 'Save failed' in scan.scan_status
        scan._refresh_interface()
        assert 'Save failed' in scan.scan_status and 'Ready to scan' in scan.scan_status
        assert np.array_equal(scan.outputChannels[0].recordingData, before_data)
        assert hw.calls == before_calls
        assert start_button.text() == 'Start scan' and start_button.isEnabled()
    if scenario == 'interface':
        # A late device/name update must not clear an acquired/loaded spectrum.
        original_channels = list(scan.outputChannels)
        original = scan.outputChannels[0].recordingData.copy()
        before_calls = list(hw.calls)
        other.name = 'DMMR_Renamed'
        QTest.qWait(650)  # real refresh timer, not only a direct helper call
        assert 'Module 5 — DMMR_Renamed' in measured.items and 'Module 5 — DMMR_M05' not in measured.items
        assert measured.value == 'Module 3 — DMMR_M03'
        assert scan.outputChannels == original_channels
        assert np.array_equal(scan.outputChannels[0].recordingData, original)
        assert hw.calls == before_calls
        sources_widget.hide()
        plot_widget.hide()
        window.resize(400, 600)
        QTest.qWait(50)
        window.grab().save(str(target / 'mscan-compact.png'))
        # Multiline supply summaries must remain visible at compact width / high DPI.
        amx.psu_ch23 = 'PSU_A'
        scan._refresh_interface()
        QTest.qWait(30)
        for case in ('shared', 'two-psus'):
            if case == 'two-psus':
                second = psum.PSUDevice.__new__(psum.PSUDevice)
                device_fields(second, 'PSU_B', 'V', core.INOUT.IN, True)
                second.startup_timeout_s, second.poll_timeout_s, second.interlock_monitoring = 1., .1, True
                second.controller = psum.PSUController(second)
                second.controller.lock, second.controller.initialized = Lock(), True
                second.controller.print = second.print
                for i in (0, 1):
                    c = psum.PSUChannel(second, second.tree)
                    second.tree.setColumnCount(len(c.parameters))
                    second.tree.addTopLevelItem(c)
                    second.channels.append(c)
                    c.initGUI({'Name': f'PSU_B_CH{i}', 'CH': str(i)})
                h2 = second.controller.device = Hardware()
                h2.enabled, h2.outputs, h2.interlocks = True, (True, True), (True, True)
                h2.voltages, h2.currents = {0: 90., 1: 95.}, {0: .002, 1: .003}
                h2.voltage_limits = {0: 250., 1: 150.}
                second.loading = False
                controller._update_state()
                second.controller._update_state()
                amx.psu_ch23 = 'PSU_B'
                scan.amx_outputs = 'CH2-CH3'
                scan._refresh_interface()
                assert scan.settingsMgr.settings[scan.AMPLITUDE_LIMITS].value == '0 – 150'
                final = scan.settingsMgr.settings[scan.FINAL_VOLTAGES].value
                assert final.splitlines() == ['PSU_B_CH0: +90 V, Iget 1 mA', 'PSU_B_CH1: −95 V, Iget 1.5 mA'], final
                assert 'PSU_B CH1: 3 mA' in scan.settingsMgr.settings[scan.CURRENT_LIMITS].value
                assert len(scan._preflight()['rails']) == 2
                QTest.qWait(30)
            setting = scan.settingsMgr.settings[scan.SUPPLIES]
            label = setting.label
            bounds = label.fontMetrics().boundingRect(QRect(0, 0, label.width(), 10000),
                Qt.TextFlag.TextWordWrap, label.text())
            assert label.height() >= bounds.height(), (case, label.text(), label.size(), bounds)
            window.grab().save(str(target / f'mscan-compact-{case}.png'))
        amx.psu_ch23 = 'None'
        scan.amx_outputs = 'CH0-CH1'
        # Labels remain human-readable after another real settings reconstruction.
        scan.saveSettings(file=target / 'roundtrip.ini')
        scan.loadSettings(file=target / 'roundtrip.ini')
        assert scan.displayDefault == 'DMMR_M03'
        assert scan.settingsMgr.settings[scan.DETECTOR].text(0) == 'DMMR module'
        assert scan.channelTree is None
        # Select another physical module through the actual combo, then run a
        # second scan: module 5 must supply its own (doubled) current, not M03's.
        controller._update_state()
        choice = scan.settingsMgr.settings[scan.DETECTOR].combo
        choice.setCurrentText('Module 5 — DMMR_Renamed')
        assert scan.outputChannels[0].sourceChannel is other
        scan.recording = True
        scan.initData()
        assert scan.initScan(), scan.scan_status
        scan.finished = False
        timer.start(8)
        from threading import Thread
        worker = Thread(target=scan.runScan, args=(lambda: scan.recording,))
        worker.start()
        deadline = time.monotonic() + 12
        while worker.is_alive() and time.monotonic() < deadline:
            QTest.qWait(2)
        worker.join(.5)
        timer.stop()
        app.processEvents()
        assert not worker.is_alive() and scan._validation['status'] == 'completed', scan._validation
        assert [c.name for c in scan.outputChannels] == ['DMMR_Renamed']
        assert scan.outputChannels[0].recordingData == pytest.approx(
            [2 * math_value(v) for v in (60, 70, 80)], rel=1e-6, abs=1e-20)
        assert scan._plan['metadata']['detector'] == dict(device='DMMR', module=5, channel='DMMR_Renamed',
                                                       interval_ms=detector_device.interval)
        scan.saveData(target / 'module-5.mscan.h5')
        with h5py.File(target / 'module-5.mscan.h5') as f:
            assert list(f['MScan/Output Channels']) == ['DMMR_Renamed']
        scan.finished = True
    if scenario == 'dock':
        from PyQt6.QtWidgets import QMainWindow
        dock_window = QMainWindow()
        app.mainWindow = manager.mainWindow = dock_window
        manager.finalizing, manager.tabBars = True, []
        manager.styleSheet = app.styleSheet()
        scan.mainDisplayWidget, scan.display.mainDisplayWidget = scan_widget, plot_widget
        for plugin in (scan, scan.display):
            plugin.dock = core.DockWidget(plugin)
            dock_window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, plugin.dock)
            plugin.initializedDock = True
            plugin.provideDock()
            plugin.dock.toggleTitleBar()
            assert plugin.dock.title == scan.TITLE
            assert plugin.dock.windowTitle() == scan.TITLE
            assert plugin.titleBarLabel.text() == scan.TITLE
            assert plugin.name != 'msScan'  # technical plugin/storage identity is preserved
        dock_window.splitDockWidget(scan.dock, scan.display.dock, Qt.Orientation.Horizontal)
        dock_window.resize(1100, 700)
        dock_window.show()
        deadline = time.monotonic() + 2
        while not scan.display.canvas._has_drawn and time.monotonic() < deadline:
            QTest.qWait(10)
        assert scan.display.canvas.isVisible() and scan.display.canvas._has_drawn
        dock_window.grab().save(str(target / 'native-docks.png'))
        dock_window.hide()
    scan.closeGUI()
    assert not scan._interface_timer.isActive()
    window.close()
    return 0


if __name__ == '__main__':
    # Like the host main(), keep QApplication alive until after its widgets are
    # destroyed. run() must not drop it while QtAgg canvases/queued layouts still
    # exist in Python cycles. Drain deferred deletes before interpreter teardown.
    applications = []
    try:
        result = run(sys.argv[1], Path(sys.argv[2]), applications)
        print('QT_SCENARIO_COMPLETE', result)
    finally:
        if applications:
            from PyQt6.QtCore import QCoreApplication, QEvent
            for widget in applications[0].topLevelWidgets():
                widget.hide()
                widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            applications[0].processEvents()
    raise SystemExit(result)
