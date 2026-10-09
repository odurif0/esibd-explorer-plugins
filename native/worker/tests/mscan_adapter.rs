#![cfg(feature = "mscan")]

// Concurrent family feature builds need distinct CARGO_TARGET_DIRs, since
// Cargo's un-hashed CARGO_BIN_EXE worker path is replaced by each feature build.

use esibd_native_worker::mscan::amplitude_steps;
use serde_json::{Value, json};
use std::{path::PathBuf, process::Command};

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_owned()
}

#[test]
fn plans_match_actual_numpy_reference_over_a_frozen_deterministic_corpus() {
    let script = r#"
import ast, json, math, sys
from pathlib import Path
import numpy as np
tree = ast.parse(Path(sys.argv[1]).read_text())
function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'amplitude_steps')
namespace = {'math': math, 'np': np, 'ScanError': RuntimeError}
exec(compile(ast.Module(body=[function], type_ignores=[]), '<reference-amplitude_steps>', 'exec'), namespace)
cases = json.loads(sys.argv[2])
out = []
for case in cases:
    try:
        out.append({'steps': namespace['amplitude_steps'](*case).tolist()})
    except RuntimeError:
        out.append({'error': True})
print(json.dumps(out, allow_nan=False))
"#;
    let mut cases = vec![
        [0.0, 1.0, 0.4],
        [1.0, 0.0, 0.4],
        [0.0, 0.3, 0.1],
        [0.0, 0.0, 1.0],
        [-1.0, 1.0, 1.0],
        [0.0, 10.0, 0.0],
        [0.0, 10.0, -1.0],
        [0.0, 10.0, 20.0],
        [0.0, 1e6, 1e-9],
        [10.0, 20.0, 10.000000000000002],
    ];
    let mut rng = 12345u64;
    for i in 0..128 {
        rng = rng.wrapping_mul(6364136223846793005).wrapping_add(1);
        let start = (rng >> 32) as f64 / u32::MAX as f64 * 100.0;
        let span = 0.5 + i as f64 * 0.75;
        let stop = start + span;
        let step = span / (2 + i % 17) as f64 * if i % 3 == 0 { 1.1 } else { 1.0 };
        cases.push(if i % 2 == 0 {
            [start, stop, step]
        } else {
            [stop, start, step]
        });
    }
    let output = Command::new("python3")
        .args(["-c", script])
        .arg(root().join("mscan/mscan_plugin.py"))
        .arg(json!(cases).to_string())
        .output()
        .expect("python3 and numpy are needed for local reference parity");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let expected: Vec<Value> = serde_json::from_slice(&output.stdout).unwrap();
    for (input, expected) in cases.iter().zip(expected) {
        let actual = amplitude_steps(input[0], input[1], input[2]);
        if expected.get("error").is_some() {
            assert!(actual.is_err(), "{input:?}");
            continue;
        }
        let actual = actual.unwrap();
        let expected = expected["steps"].as_array().unwrap();
        assert_eq!(actual.len(), expected.len(), "{input:?}");
        for (got, want) in actual.iter().zip(expected) {
            assert!(
                (got - want.as_f64().unwrap()).abs() < 1e-10,
                "{input:?}: {got} != {want}"
            );
        }
    }
}

#[test]
fn real_worker_supervisor_and_explorer_adapter_match_reference_and_fail_closed() {
    let script = r#"
import ast, importlib.util, math, subprocess, sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace as NS
import numpy as np
import pytest
root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'tests'))
import test_mscan as fixtures
from test_mscan_continuous import continuous
from test_mscan_offset import add_offset

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

transport = load('native_transport_test', root / 'native/python/_native_worker.py')
adapter_module = load('mscan_adapter_test', root / 'mscan/_runtime/_native_scan.py')
mp = pytest.MonkeyPatch()
module = fixtures.module.__wrapped__(mp)
if hasattr(module.MScan, '_run_scan_python_reference'):
    reference_runner = module.MScan._run_scan_python_reference
else:
    # During parent integration, production runScan may already be native.
    # Read the immutable tracked baseline without replacing workspace files.
    source = subprocess.check_output(['git','show','HEAD:mscan/mscan_plugin.py'], cwd=root, text=True)
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MScan')
    function = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'runScan')
    namespace = dict(module.__dict__)
    exec(compile(ast.Module(body=[function],type_ignores=[]), '<tracked-reference-runScan>', 'exec'), namespace)
    reference_runner = namespace['runScan']

def rig(mode, fault=None, offset=False):
    r = fixtures.rig.__wrapped__(module)
    c = continuous.__wrapped__(r, mp)
    s = c.scan
    s.scan_mode = mode
    if offset:
        add_offset(r, factor=1/3)
    s._plan = s._preflight()
    if offset:
        s._validation.update(offset_v=np.full(3, np.nan), offset_target=np.full(3, np.nan))
    if fault == 'late_gui':
        command = s._command
        def delayed(targets, **kwargs):
            if targets == [20.,20.]:
                c.clock.now += .1
            return command(targets, **kwargs)
        s._command = delayed
    fired = [False]
    def hook():
        if offset:
            r.offset.settle()
        if r.current() != 20 or fired[0]:
            return
        fired[0] = True
        if fault == 'stop':
            s._cancel.set()
        elif fault == 'current':
            r.psus[0].channels[0].current_readback = .01
        elif fault == 'external':
            r.psus[0].channels[0].voltage_request_revision += 1
        elif fault == 'clock':
            c.clock.jump += 1
        elif fault == 'detector':
            r.detector.module_address = lambda: 5
        elif fault == 'offset':
            r.offset.channel._value = 9
    c.tick = hook
    return c

checked = 0
try:
    for mode in [module.MScan.STEPPED, module.MScan.CONTINUOUS]:
        for fault, offset in [(None,False),(None,True),('stop',False),('current',False),
                              ('external',False),('clock',False),('detector',False),('offset',True),('late_gui',False)]:
            reference = rig(mode, fault, offset)
            reference_runner(reference.scan, lambda: True)
            reference_data = reference.scan.outputChannels[0].recordingData.copy()
            reference_status = reference.scan._validation['status']
            reference_writes = list(reference.rig.psus[0].writes)
            reference_offset = list(reference.rig.offset.channel.writes) if offset else None
            for raw_json in [False, True]:
                native = rig(mode, fault, offset)
                adapter_module.time = module.time
                proxy = transport.NativeWorkerProxy(root/'mscan', 'mscan', {},
                    command=[sys.argv[2], '--family', 'mscan'], startup_timeout_s=2)
                try:
                    rpc = proxy.call_json if raw_json else proxy.call_method
                    adapter = adapter_module.NativeScanAdapter(native.scan, rpc, raw_json=raw_json)
                    result = adapter.run()
                    assert result['validation']['status'] == reference_status, (mode, fault, result)
                    assert result['data'] == pytest.approx(reference_data, nan_ok=True), (mode, fault, result['data'],reference_data)
                    assert native.rig.psus[0].writes == reference_writes, (mode, fault, native.rig.psus[0].writes, reference_writes)
                    assert native.rig.psus[1].writes == []
                    assert native.rig.psus[0].isOn()
                    assert not adapter._running.is_set()
                    if offset:
                        assert native.rig.offset.channel.writes == reference_offset
                        assert result['validation']['offset_target'] == pytest.approx(reference.scan._validation['offset_target'], nan_ok=True)
                    if reference_status != 'completed':
                        assert not any(target > 30 for _, target in native.rig.psus[0].writes)
                    else:
                        assert result['data'] == pytest.approx([10,20,30])
                        page = adapter.export_result(page_size=1)
                        assert page['data'] == result['data']
                        if mode == module.MScan.CONTINUOUS:
                            raw = result['validation']['continuous']
                            assert len(raw['psu_time']) == len(raw['psu_v'])
                            assert len(raw['detector_current']) > 0
                            assert np.all(np.diff(raw['detector_time']) > 0)
                            for key, values in raw.items():
                                np.testing.assert_array_equal(page['validation']['continuous'][key], values)
                    old_session = result['session']
                    adapter.reset()
                    assert proxy.get_attribute('state') == 'idle'
                    checked += 1
                finally:
                    proxy.close(grace_s=0)
    # Queued actions are never executed after cancellation or twice, even when
    # invoking the adapter directly rather than through the native loop.
    native = rig(module.MScan.STEPPED)
    adapter_module.time = module.time
    proxy = transport.NativeWorkerProxy(root/'mscan', 'mscan', {}, command=[sys.argv[2],'--family','mscan'])
    try:
        adapter = adapter_module.NativeScanAdapter(native.scan,proxy.call_method)
        status = adapter.start()
        action = status['action']
        adapter.cancel()
        with pytest.raises(RuntimeError):
            adapter._command_on_gui(action)
        assert native.rig.psus[0].writes == []
        assert adapter.step()['status'] == 'stopped'
        adapter.reset()
        native.scan._cancel.clear()
        status = adapter.start()
        action = status['action']
        adapter._command_on_gui(action)
        with pytest.raises(RuntimeError):
            adapter._command_on_gui(action)
        assert native.rig.psus[0].writes == [(0,10.0),(1,10.0)]
        adapter.cancel()
        adapter.step()
    finally:
        proxy.close(grace_s=0)
    # A corrupt data model must fail before the first Explorer voltage write.
    native = rig(module.MScan.STEPPED)
    adapter_module.time = module.time
    proxy = transport.NativeWorkerProxy(root/'mscan','mscan',{},command=[sys.argv[2],'--family','mscan'])
    try:
        native.scan.inputChannels[0].recordingData[0] = 11
        adapter = adapter_module.NativeScanAdapter(native.scan,proxy.call_method)
        with pytest.raises(RuntimeError, match='amplitude axis differs'):
            adapter.run()
        assert native.rig.psus[0].writes == []
        assert native.scan._validation['status'] == 'error'
    finally:
        proxy.close(grace_s=0)
    # A process loss retains acknowledged points and never restores/retries.
    native = rig(module.MScan.STEPPED)
    adapter_module.time = module.time
    proxy = transport.NativeWorkerProxy(root/'mscan','mscan',{},command=[sys.argv[2],'--family','mscan'])
    try:
        def kill():
            if native.rig.current() == 20 and proxy._process.poll() is None:
                proxy._process.kill()
                proxy._process.wait(timeout=2)
        native.tick = kill
        adapter = adapter_module.NativeScanAdapter(native.scan,proxy.call_method)
        with pytest.raises(RuntimeError):
            adapter.run()
        assert native.scan._validation['status'] == 'error'
        assert native.scan.outputChannels[0].recordingData[0] == 10
        assert np.isnan(native.scan.outputChannels[0].recordingData[1:]).all()
        assert native.rig.psus[0].writes == [(0,10.),(1,10.),(0,20.),(1,20.)]
        assert not adapter._running.is_set()
    finally:
        proxy.close(grace_s=0)
        assert proxy._process.poll() is not None
    print(f'{checked} paired adapter/reference executions plus queued-cancel/duplicate-write/model-shape/crash checks passed')
finally:
    mp.undo()
"#;
    let output = Command::new("python3")
        .args(["-c", script])
        .arg(root())
        .arg(env!("CARGO_BIN_EXE_esibd-native-worker"))
        .env("QT_QPA_PLATFORM", "offscreen")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .output()
        .expect("python3 is needed for the Explorer adapter test");
    assert!(
        output.status.success(),
        "stdout: {}\nstderr: {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    println!("{}", String::from_utf8_lossy(&output.stdout));
}

#[test]
fn production_entrypoint_stops_and_reaps_actual_packaged_linux_worker() {
    if !cfg!(target_os = "linux") {
        return;
    }
    let script = r#"
import sys, tempfile
from pathlib import Path
from types import SimpleNamespace as NS
import math
import numpy as np
import pytest
root = Path(sys.argv[1])
sys.path.insert(0,str(root/'tests'))
import test_mscan as fixtures
from test_mscan_continuous import continuous
mp = pytest.MonkeyPatch()
module = fixtures.module.__wrapped__(mp)
try:
    r = fixtures.rig.__wrapped__(module)
    c = continuous.__wrapped__(r,mp)
    s = c.scan
    s.scan_mode = s.STEPPED
    s._plan = s._preflight()
    native_module = module._load_native_runtime('_native_worker')
    scan_adapter = module._load_native_runtime('_native_scan')
    mp.setattr(scan_adapter,'time',module.time)
    created = []
    Original = native_module.NativeWorkerProxy
    class Captured(Original):
        def __init__(self,*args,**kwargs):
            assert 'command' not in kwargs  # manifest-verified deployment, not a test override
            super().__init__(*args,**kwargs)
            created.append(self)
    mp.setattr(native_module,'NativeWorkerProxy',Captured)
    def update(done):
        if not done and math.isfinite(s.outputChannels[0].recordingData[0]):
            s.recording = False  # production setter must cancel the pending next action
    s.signalComm.scanUpdateSignal.emit = update
    with tempfile.TemporaryDirectory(prefix='mscan-entrypoint-') as data:
        s.pluginManager.Settings = NS(dataPath=Path(data))
        # Bypass the fixture's reference-only instance binding deliberately.
        s._queue_completion = lambda action: s._gui(action, cancel=False)  # test-only synchronous GUI fixture
        module.MScan.runScan(s,lambda: True)
        assert s._validation['status'] == 'stopped', s._validation
        assert s.outputChannels[0].recordingData[0] == 10
        assert np.isnan(s.outputChannels[0].recordingData[1:]).all()
        assert r.psus[0].writes == [(0,10.),(1,10.)]
        assert r.psus[1].writes == []
        assert r.psus[0].isOn()
        assert len(created) == 1
        worker = created[0]
        assert worker._process.poll() is not None
        assert worker._closed
        assert s._native_worker is None
        assert worker._command[0] == str(native_module.executable(root/'mscan','mscan'))
        assert list(Path(data).glob('logs/mscan/native/mscan/*.jsonl'))
    print('packaged Linux runScan/recording-stop/reap/log-path checks passed')
finally:
    mp.undo()
"#;
    let output = Command::new("python3")
        .args(["-c", script])
        .arg(root())
        .env("QT_QPA_PLATFORM", "offscreen")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "stdout: {}\nstderr: {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    println!("{}", String::from_utf8_lossy(&output.stdout));
}
