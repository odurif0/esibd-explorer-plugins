#![cfg(feature = "transmission")]

use std::{
    io::Read,
    path::Path,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

// Exercise the real framed subprocess and the actual Python facade, not a
// successful fake transport. No host, Qt, DLL or hardware is required here.
#[test]
fn python_adapter_reference_parity_and_actual_worker_faults() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let script = r#"
import importlib
import importlib.util
import json
import math
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path
import numpy as np

root, executable = Path(sys.argv[1]), sys.argv[2]
def load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
load('_native_transmission_test', root / 'transmission/_runtime/__init__.py', True)
engine = importlib.import_module('_native_transmission_test._engine')
native = importlib.import_module('_native_transmission_test._native_engine')
sim = importlib.import_module('_native_transmission_test._simulator')
beamline = importlib.import_module('_native_transmission_test._beamline')
logmod = importlib.import_module('_native_transmission_test._log')
proxy = load('_transmission_actual_proxy', root / 'native/python/_native_worker.py')

text = '''
settle_s = 0.5
average_s = 1.0
seed = 2
reference_every = 3
verify_pairs = 2
[pressure]
P_funnel = [2.0, 6.0]
[[stage]]
name = "Funnel"
aperture = "A1"
downstream = ["A2", "A3", "A4", "Collector"]
budget = 40
[[stage.knob]]
name = "Inlet"
channels = { Inlet = 1.0 }
window = [-110.0, 270.0]
max_step = 10.0
[[stage.knob]]
name = "Funnel exit"
channels = { Funnel_exit = 1.0 }
window = [-15.0, 15.0]
max_step = 1.5
'''

def worker():
    return proxy.NativeWorkerProxy(root / 'transmission', 'transmission', {},
                                   command=[executable, '--family', 'transmission'], stop_grace_s=0.1)

def bounded(config, instrument):
    previous = {c: sim.KNOBS[c][2] for c in sim.KNOBS}
    steps = {}
    for stage in config.stages:
        for knob in stage.knobs:
            _, step = knob.resolve(previous)
            for channel, gain in knob.channels.items():
                steps[channel] = max(steps.get(channel, 0.0), abs(gain) * step)
    if config.target:
        for channel, gain in config.target.channels.items():
            steps[channel] = abs(gain) * config.target.max_step
    for _, command in instrument.commands:
        for channel, value in command.items():
            assert abs(value - previous[channel]) <= steps[channel] + 1e-7, (channel, previous[channel], value)
            lo, hi = sim.KNOBS[channel][:2]
            assert lo <= value <= hi
            previous[channel] = value

def run(strategy, native_mode=True, hook=None, config=None, simulation=None):
    config = config or engine.parse_config(text)
    instrument = simulation or sim.SimulatedBeamline(seed=0, noise=0.0, floor=0.0, drift_per_sqrt_hour=0.0, cancel=threading.Event())
    marks = []
    instrument.measuring = marks.append
    events, logs = [], []
    transport = worker() if native_mode else None
    def on_event(kind, data):
        events.append((kind, data))
        if hook:
            hook(kind, data, instrument, transport)
    try:
        options = dict(strategy=strategy, on_event=on_event, log=lambda event, data: logs.append((event, data)))
        optimizer = native.Optimizer(config, instrument, worker=transport, **options) if native_mode else engine.Optimizer(config, instrument, **options)
        result = optimizer.run()
        assert not marks or (len(marks) % 2 == 0 and marks[::2] == [True] * (len(marks)//2) and marks[1::2] == [False] * (len(marks)//2)), marks
        bounded(config, instrument)
        return optimizer, instrument, result, events, logs
    finally:
        if transport:
            transport.close()

reference = run('coordinate', False)
ported = run('coordinate')
assert reference[2]['status'] == ported[2]['status'] == 'completed'
assert reference[2]['stages'][0]['adopted'] == ported[2]['stages'][0]['adopted'] == True
for channel, value in reference[2]['final'].items():
    assert abs(value - ported[2]['final'][channel]) < 0.03, (channel, value, ported[2]['final'][channel])
assert len(ported[0].history) == ported[2]['evaluations']
assert isinstance(next(d['stage'] for k, d in ported[3] if k == 'stage'), engine.Stage)
assert ported[0].worker.closed

bayesian = run('bayesian')
assert bayesian[2]['status'] == 'completed', bayesian[2]
assert bayesian[2]['stages'][0]['adopted'], bayesian[2]
baseline = sim.transport(sim.SimulatedBeamline().actual)['Collector']
assert sim.transport(bayesian[1].actual)['Collector'] > 1.3 * baseline
assert any(k == 'ask' and d.get('numerics') == 'nalgebra/argmin-nelder-mead/pcg' for k, d in bayesian[4])

for failure in ('stop', 'interlock', 'off'):
    for is_native in (False, True):
        commands = []
        def hook(kind, data, instrument, transport):
            if kind == 'evaluation' and data['record']['index'] == 5:
                commands.append(len(instrument.commands))
                if failure == 'stop':
                    instrument.cancel.set()
                elif failure == 'interlock':
                    instrument.fault = 'discharge'
                else:
                    instrument.devices_on = False
        optimizer, instrument, result, events, logs = run('coordinate', is_native, hook)
        assert result['status'] == {'stop':'stopped', 'interlock':'interlock', 'off':'instrument error'}[failure], result
        if failure == 'off':
            assert len(instrument.commands) == commands[0]
            end = next(d for k, d in logs if k == 'run_end')
            assert 'Traceback' in end['traceback'] and 'InstrumentError' in end['traceback'], end
        else:
            assert all(instrument.setpoint[c] == sim.KNOBS[c][2] for c in result['initial'])
            assert any(k == 'restore' for k, d in logs)

for failure in ('empty', 'thin', 'never_closes', 'stuck_readback'):
    instrument = sim.SimulatedBeamline(seed=0, cancel=threading.Event())
    if failure == 'never_closes':
        instrument.window = lambda channels, begin, end: None
    elif failure == 'stuck_readback':
        instrument.readbacks = lambda channels: {c: -1000.0 for c in channels}
    else:
        window = instrument.window
        def sparse(channels, begin, end):
            data = window(channels, begin, end)
            return None if data is None else {c:(v[0], math.nan, 1) if failure == 'thin' else (math.nan, math.nan, 0) for c, v in data.items()}
        instrument.window = sparse
    config = engine.parse_config(text.replace('settle_s = 0.5', 'settle_s = 0.5\nsettle_timeout_s = 2.0'))
    outcome = run('coordinate', config=config, simulation=instrument)[2]
    assert outcome['status'] == 'instrument error', outcome
    assert len(instrument.commands) <= 1

instrument = sim.SimulatedBeamline(seed=0)
window = instrument.window
gaps, commands = [], []
def temporary_gap(channels, begin, end):
    data = window(channels, begin, end)
    if data is not None:
        gaps.append(1)
        commands.append(len(instrument.commands))
        if len(gaps) in (3, 4):
            return {c:(math.nan, math.nan, 0) for c in data}
    return data
instrument.window = temporary_gap
outcome = run('coordinate', simulation=instrument)
assert outcome[2]['status'] == 'completed', outcome[2]
assert commands[2] == commands[3] == commands[4]
assert outcome[0].counts['empty_windows'] == 2

# Spectra, numerical facade and paired mass-stage recovery use the same process.
with tempfile.TemporaryDirectory() as directory:
    with closing(worker()) as transport:
        amplitudes = np.linspace(150.0, 700.0, 31)
        step = float(amplitudes[1]-amplitudes[0])
        target = engine.Target({'Q2_RF':1.0}, 150.0, step, ['A3','A4','Collector'], max_step=step, spectrum_points=0)
        config = engine.Config([], None, target, settle_s=0.2, average_s=1.0)
        instrument = sim.SimulatedBeamline(seed=4)
        log = logmod.JsonlLog(Path(directory) / 'scan.jsonl')
        optimizer = native.Optimizer(config, instrument, worker=transport, log=log.write)
        result = optimizer.spectrum(amplitudes, target.measure)
        assert result['status'] == 'completed' and len(result['current']) == 31, result
        assert instrument.setpoint['Q2_RF'] == sim.KNOBS['Q2_RF'][2]
        bounded(config, instrument)
        expected = engine.pick_peak(result['amplitude'], result['current'], 500.0)
        actual = native.pick_peak(transport, result['amplitude'], result['current'], 500.0)
        assert isinstance(actual, tuple), actual
        assert np.allclose(expected, actual, atol=1e-6), (expected, actual)
        fitted = native.fit_peak(transport, [498.,499.,500.,501.,502.], [5-(x-500.4)**2 for x in [498.,499.,500.,501.,502.]], [math.nan]*5)
        assert abs(fitted[0]-5.0) < 1e-8 and abs(fitted[2]-500.4) < 1e-8
        log.close()
        records = logmod.read(log.path)
        assert records[0]['event'] == 'sweep_start' and records[-1]['event'] == 'sweep_end'
        for record in records:
            json.dumps(record, allow_nan=False)

with closing(worker()) as transport:
    config = engine.parse_config(beamline.build(sim.BEAMLINE, selected=['q1'], mode='mass', filter_key='q2', center=520., width=30.,
        settings=dict(settle_s=0.5, average_s=1.0, seed=1, verify_pairs=2)))
    instrument = sim.SimulatedBeamline(seed=1, noise=0.0, floor=0.0, drift_per_sqrt_hour=0.0, cancel=threading.Event())
    logs = []
    optimizer = native.Optimizer(config, instrument, worker=transport, log=lambda event, data: logs.append((event, data)))
    result = optimizer.run()
    assert result['status'] == 'completed', result
    assert abs(result['target']['final_center']-520.) < 10., result
    assert all(s['adopted'] for s in result['stages']), result
    assert result['checks']['after']['currents']['Collector'] > result['checks']['before']['currents']['Collector']
    assert len(result['spectra']['before']['amplitude']) == len(result['spectra']['after']['amplitude']) == 21
    assert all(r['peak']['measure'] == ['A3','A4','Collector'] for r in optimizer.history)
    assert optimizer.restore_initial() == dict(status='restored', reason='')
    assert all(instrument.setpoint[c] == sim.KNOBS[c][2] for c in optimizer.initial)
    bounded(config, instrument)
    assert optimizer.close() is True
    assert optimizer.close() is True

# Never replay a hardware action or restore automatically after an unacknowledged
# command / lost process. Acquisition guards still close on the scan thread.
with closing(worker()) as transport:
    instrument = sim.SimulatedBeamline(seed=1, cancel=threading.Event())
    commands = []
    def kill(kind, data):
        if kind == 'evaluation' and data['record']['index'] == 4:
            commands.append(len(instrument.commands))
            transport._process.kill()
            transport._process.wait(timeout=5.)
    optimizer = native.Optimizer(engine.parse_config(text), instrument, worker=transport, strategy='coordinate', on_event=kill)
    try:
        optimizer.run()
        raise AssertionError('Worker death must not produce a successful outcome')
    except RuntimeError:
        pass
    assert len(instrument.commands) == commands[0]
    assert optimizer.status == 'instrument error' and optimizer._failed
    try:
        optimizer.restore_initial()
        raise AssertionError('Poisoned native session must not send another write')
    except engine.InstrumentError:
        pass

with closing(worker()) as transport:
    instrument = sim.SimulatedBeamline(seed=0, noise=0., floor=0., drift_per_sqrt_hour=0.)
    optimizer = native.Optimizer(engine.parse_config(text), instrument, worker=transport, strategy='coordinate',
                                 log=lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    assert optimizer.run()['status'] == 'completed'

print('Transmission: real subprocess, coordinate/reference tolerance, Bayesian improvement, stop/interlock/off, '
      'missing/thin/late/stuck data, gap retry, strict JSONL, spectrum/peak/undo and lost-worker checks passed')
"#;
    let mut child = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(root)
        .arg(env!("CARGO_BIN_EXE_esibd-native-worker"))
        .env("OPENBLAS_NUM_THREADS", "1")
        .env("OMP_NUM_THREADS", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("Python reference test must start");
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let out = thread::spawn(move || {
        let mut s = String::new();
        let mut f = stdout;
        f.read_to_string(&mut s).unwrap();
        s
    });
    let err = thread::spawn(move || {
        let mut s = String::new();
        let mut f = stderr;
        f.read_to_string(&mut s).unwrap();
        s
    });
    let deadline = Instant::now() + Duration::from_secs(180);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("Python/native Transmission parity test exceeded 180s");
        }
        thread::sleep(Duration::from_millis(20));
    };
    let stdout = out.join().unwrap();
    let stderr = err.join().unwrap();
    assert!(
        status.success(),
        "Python parity failed: {status}\n{stdout}\n{stderr}"
    );
    assert!(stdout.contains("checks passed"), "{stdout}\n{stderr}");
}
