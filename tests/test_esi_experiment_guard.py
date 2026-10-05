"""Filesystem/process proofs only: no runtime import, COM or instrument access."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

HELPER = Path(__file__).resolve().parents[1] / 'esi/_experiment_guard.py'


@pytest.fixture
def guard_module(monkeypatch):
    spec = importlib.util.spec_from_file_location('_esi_guard_test', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Independent simulated kernel, even when other tests loaded plugin runtimes.
    monkeypatch.setattr(module, 'sys', SimpleNamespace(modules={}))
    return module


@pytest.fixture
def env(tmp_path, guard_module):
    guards = []
    def create(**kw):
        options = dict(plugin_dir=tmp_path / 'plugins/esi', com=16, kind='heater',
                       output_dir=tmp_path / 'outputs', kernel_globals={}, state_root=tmp_path / 'state')
        options.update(kw)
        guard = guard_module.ExperimentGuard(**options)
        guards.append(guard)
        return guard
    yield create
    # Teardown only: production callers cannot discard uncertain ownership.
    for guard in guards:
        if not guard._released:
            guard._release_unused()


def initial(module, guard, name='new'):
    directory = guard.output_dir / name
    directory.mkdir(parents=True)
    report = directory / ('report.json' if guard.kind == 'heater' else 'metadata.json')
    data = dict(started_utc=module.utc(), shutdown_confirmed=False, operator_restarts=guard._records, esi_com=guard.com)
    module.durable_new_file(directory / 'samples.csv', b'temperature_c\n')
    module.save_json(report, data)
    return directory, report, data


def legacy(module, directory, *, kind='heater', named=True, report_shutdown=False):
    directory.mkdir(parents=True, exist_ok=True)
    run = directory / 'old-run'
    run.mkdir()
    start, stamp, end = ('2026-09-29T10:00:00+00:00', '2026-09-29T10:00:01+00:00', '2026-09-29T10:01:00+00:00')
    marker = directory / (module.HEATER_MARKER if kind == 'heater' else module.PT_MARKER)
    report = run / ('report.json' if kind == 'heater' else 'metadata.json')
    module.save_json(report, dict(started_utc=start, ended_utc=end, shutdown_confirmed=report_shutdown, esi_com=16))
    module.durable_new_file(run / 'samples.csv', b'temperature_c\n21.0\n')
    data = dict(started_utc=start if named else stamp)
    if named:
        data['run_directory'] = run.name
    module.save_json(marker, data)
    return marker, report


def agree(prompt):
    return 'RESTART ' + prompt.split('RESTART ')[1].split(':')[0]


def complete(module, guard, report, data):
    data.update(shutdown_confirmed=True, pressure_closed=True)
    module.save_json(report, data)
    return guard.finalize(shutdown_confirmed=True, owners_idle=True, auxiliary_closed=True, transport_poisoned=False)


def test_claim_and_confirmed_finish(guard_module, env):
    g = env()
    assert g.authorize_restart() == []
    directory, report, data = initial(guard_module, g)
    claim = g.claim(directory, report)
    assert claim['claim_id'] and claim['operator_restarts'] == []
    g.mark_hardware_started()
    assert complete(guard_module, g, report, data)
    assert not g.registry.exists() and not (g.heater_dir / guard_module.HEATER_MARKER).exists()
    assert g._released


@pytest.mark.parametrize('com', [True, False, 0, 256, 16., 'COM16', None])
def test_bad_port_refused(env, com):
    with pytest.raises(ValueError):
        env(com=com)


@pytest.mark.parametrize('key', ['_HC_GUARD', '_PT_GUARD', '_ADC_GUARD'])
def test_old_kernel_rejected_before_locks(env, key):
    with pytest.raises(RuntimeError, match='kernel'):
        env(kernel_globals={key: {'run': object()}})
    assert env().abandon_before_hardware()


def test_preloaded_runtime_refused_without_lock_leak(guard_module, env):
    guard_module.sys.modules['_esibd_bundled_esi'] = SimpleNamespace()
    with pytest.raises(RuntimeError, match='namespace'):
        env()
    guard_module.sys.modules.clear()
    assert env().abandon_before_hardware()


def test_same_port_across_copies_excludes(env, tmp_path):
    env()
    with pytest.raises(RuntimeError, match='Another owner'):
        env(plugin_dir=tmp_path / 'second-copy/esi', output_dir=tmp_path / 'other')


def test_old_heater_lock_excludes_pt(guard_module, env, tmp_path):
    lock = guard_module.RunLock(tmp_path / 'plugins/esi/logs/esi_heater_characterization')
    try:
        with pytest.raises(RuntimeError, match='Another owner'):
            env(kind='pressure_temperature')
    finally:
        lock.close()
    assert env().abandon_before_hardware()  # COM lock wasn't leaked by the refusal.


@pytest.mark.parametrize('kind', ['heater', 'pressure_temperature'])
def test_old_marker_requires_evidence_and_declaration(guard_module, env, tmp_path, kind):
    directory = (tmp_path / 'plugins/esi/logs/esi_heater_characterization' if kind == 'heater'
                 else tmp_path / 'plugins/notebooks/pressure_temperature_runs')
    marker, old_report = legacy(guard_module, directory, kind=kind, named=kind == 'heater')
    before = marker.read_bytes(), old_report.read_bytes()
    g = env(output_dir=tmp_path / 'changed-output')
    with pytest.raises(RuntimeError, match='declaration'):
        g.authorize_restart(input_fn=lambda _: '')
    assert before == (marker.read_bytes(), old_report.read_bytes())
    records = g.authorize_restart(input_fn=agree)
    assert records[0]['shutdown_confirmed'] is False
    assert records[0]['process_termination_verified_by_software'] is False
    d, r, data = initial(guard_module, g)
    g.claim(d, r)
    assert old_report.read_bytes() == before[1]
    archive = Path(records[0]['archive'])
    assert before[0] in [p.read_bytes() for p in (archive / 'markers').iterdir()]
    assert complete(guard_module, g, r, data)
    assert old_report.read_bytes() == before[1]


@pytest.mark.parametrize('missing', ['report.json', 'samples.csv'])
def test_missing_legacy_evidence_is_recorded_and_still_needs_the_declaration(guard_module, env, tmp_path, missing):
    directory = tmp_path / 'plugins/esi/logs/esi_heater_characterization'
    marker, report = legacy(guard_module, directory)
    (report.parent / missing).unlink()
    remaining = {path.name: path.read_bytes() for path in report.parent.iterdir()}
    g = env()
    with pytest.raises(RuntimeError, match='declaration'):
        g.authorize_restart(input_fn=lambda _: '')
    record = g.authorize_restart(input_fn=agree)[0]
    assert missing in record['missing_evidence'][0]
    archive = Path(record['archive'])
    assert [path.name for path in (archive / 'missing').iterdir()] == ['000.json']
    assert {path.name: path.read_bytes() for path in report.parent.iterdir()} == remaining  # nothing rewritten
    d, r, data = initial(guard_module, g)
    g.claim(d, r)
    assert complete(guard_module, g, r, data)
    assert not marker.exists()


def test_deleted_run_folder_after_unconfirmed_shutdown_needs_declaration_not_manual_cleanup(guard_module, env, tmp_path):
    # Lab case: a run kept its claim (shutdown unconfirmed), then its folder was deleted.
    g = env(kind='pressure_temperature')
    g.authorize_restart()
    d, r, data = initial(guard_module, g)
    g.claim(d, r)
    g.mark_hardware_started()
    assert g.finalize(shutdown_confirmed=False, owners_idle=True, auxiliary_closed=True, transport_poisoned=False) is False
    g._release_unused()  # TEST ONLY: the old kernel exited
    for path in sorted(d.rglob('*'), reverse=True):
        path.unlink()
    d.rmdir()
    fresh = env(kind='pressure_temperature')
    prompts = []
    record = fresh.authorize_restart(input_fn=lambda prompt: prompts.append(prompt) or agree(prompt))[0]
    assert prompts and record['physical_safety_declared'] is True
    assert any('no longer exists' in absence for absence in record['missing_evidence'])
    d2, r2, data2 = initial(guard_module, fresh, 'restarted')
    fresh.claim(d2, r2)
    assert complete(guard_module, fresh, r2, data2)


def test_adc_probe_shares_the_com_guard_with_the_heater_notebooks(guard_module, env, tmp_path):
    probe = env(kind='adc_probe', output_dir=tmp_path / 'esi_adc_probe_runs')
    assert probe.authorize_restart() == []
    d, r, data = initial(guard_module, probe)
    assert r.name == 'metadata.json'
    probe.claim(d, r)
    probe.mark_hardware_started()
    # Discharge unconfirmed: the probe keeps the claim, like any ESI notebook.
    assert probe.finalize(shutdown_confirmed=False, owners_idle=True, auxiliary_closed=True, transport_poisoned=False) is False
    probe._release_unused()  # TEST ONLY: the old kernel exited
    pt = env(kind='pressure_temperature')
    with pytest.raises(RuntimeError, match='declaration'):
        pt.authorize_restart(input_fn=lambda _: '')
    record = pt.authorize_restart(input_fn=agree)[0]
    assert record['missing_evidence'] == []
    d2, r2, data2 = initial(guard_module, pt, 'after-probe')
    pt.claim(d2, r2)
    assert complete(guard_module, pt, r2, data2)
    assert r.read_bytes() and json.loads(r.read_text())['shutdown_confirmed'] is False  # evidence untouched


def test_ambiguous_timestamp_only_pt_refuses(guard_module, env, tmp_path):
    directory = tmp_path / 'plugins/notebooks/pressure_temperature_runs'
    _, report = legacy(guard_module, directory, kind='pressure_temperature', named=False)
    duplicate = directory / 'other-run'
    duplicate.mkdir()
    (duplicate / 'metadata.json').write_bytes(report.read_bytes())
    with pytest.raises(RuntimeError, match='exactly one'):
        env().authorize_restart(input_fn=agree)


def test_evidence_changed_during_consent_refuses(guard_module, env, tmp_path):
    _, report = legacy(guard_module, tmp_path / 'plugins/esi/logs/esi_heater_characterization')
    def change(prompt):
        (report.parent / 'samples.csv').write_text('changed')
        return agree(prompt)
    with pytest.raises(RuntimeError, match='changed'):
        env().authorize_restart(input_fn=change)


@pytest.mark.parametrize('field,value', [(name, value) for name in ('shutdown_confirmed', 'owners_idle', 'auxiliary_closed')
                                       for value in (False, None, 1, 'True')]
                                      + [('transport_poisoned', value) for value in (True, None, 0, 'False')])
def test_uncertain_or_nonboolean_facts_never_release(guard_module, env, field, value):
    g = env()
    g.authorize_restart()
    d, r, data = initial(guard_module, g)
    g.claim(d, r)
    g.mark_hardware_started()
    data['shutdown_confirmed'] = True
    guard_module.save_json(r, data)
    facts = dict(shutdown_confirmed=True, owners_idle=True, auxiliary_closed=True, transport_poisoned=False)
    facts[field] = value
    assert g.finalize(**facts) is False
    assert g.registry.exists() and not g._released
    with pytest.raises(RuntimeError, match='constructor'):
        g.abandon_before_hardware()


def test_final_report_must_be_durable_and_concordant(guard_module, env):
    g = env()
    g.authorize_restart()
    d, r, _ = initial(guard_module, g)
    g.claim(d, r)
    with pytest.raises(RuntimeError, match='Final report'):
        g.finalize(shutdown_confirmed=True, owners_idle=True, auxiliary_closed=True, transport_poisoned=False)
    assert g.registry.exists()


def test_changed_claim_refuses_release(guard_module, env):
    g = env()
    g.authorize_restart()
    d, r, data = initial(guard_module, g)
    g.claim(d, r)
    changed = dict(g._claim, claim_id='another-owner')
    guard_module.save_json(g.registry, changed)
    with pytest.raises(RuntimeError, match='claim changed'):
        complete(guard_module, g, r, data)
    assert g.registry.exists()


def test_partial_claim_never_becomes_clean_abandon(guard_module, env, monkeypatch):
    g = env()
    g.authorize_restart()
    d, r, _ = initial(guard_module, g)
    save = guard_module.save_json
    def interrupted(path, data):
        if path != g.registry:
            raise KeyboardInterrupt()
        return save(path, data)
    monkeypatch.setattr(guard_module, 'save_json', interrupted)
    with pytest.raises(KeyboardInterrupt):
        g.claim(d, r)
    assert g.registry.exists()
    assert g.abandon_before_hardware() is False
    assert not g._released


def test_actual_subprocess_cannot_take_live_port(guard_module, env, tmp_path):
    g = env()
    code = '''import importlib.util,sys
s=importlib.util.spec_from_file_location('probe',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
try:
 m.ExperimentGuard(plugin_dir=sys.argv[2],com=16,kind='heater',output_dir=sys.argv[3],kernel_globals={},state_root=sys.argv[4])
except RuntimeError:
 sys.exit(0)
sys.exit(5)
'''
    result = subprocess.run([sys.executable, '-c', code, str(HELPER), str(tmp_path / 'copy/esi'),
                             str(tmp_path / 'other'), str(g.state_dir.parent)], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode()


def test_registered_claim_survives_plugin_copy_and_output_change(guard_module, env, tmp_path):
    old = env()
    old.authorize_restart()
    d, r, _ = initial(guard_module, old)
    old.claim(d, r)
    old.mark_hardware_started()
    old._release_unused()  # Simulated terminated process; persistent uncertainty stays.
    new = env(plugin_dir=tmp_path / 'copy/esi', output_dir=tmp_path / 'new-output', kind='pressure_temperature')
    with pytest.raises(RuntimeError, match='declaration'):
        new.authorize_restart(input_fn=lambda _: '')
    records = new.authorize_restart(input_fn=agree)
    assert records and json.loads(r.read_text())['shutdown_confirmed'] is False
    d2, r2, data2 = initial(guard_module, new)
    new.claim(d2, r2)
    assert json.loads((old.heater_dir / guard_module.HEATER_MARKER).read_text())['claim_id'] == new._claim['claim_id']
    assert complete(guard_module, new, r2, data2)
    assert not (old.heater_dir / guard_module.HEATER_MARKER).exists()
    assert json.loads(r.read_text())['shutdown_confirmed'] is False


def test_historical_registered_root_lock_is_also_acquired(guard_module, env, tmp_path):
    old = env()
    old.authorize_restart()
    d, r, _ = initial(guard_module, old)
    old.claim(d, r)
    old._release_unused()
    legacy_lock = guard_module.RunLock(old.heater_dir)
    try:
        with pytest.raises(RuntimeError, match='Another owner'):
            env(plugin_dir=tmp_path / 'copy/esi')
    finally:
        legacy_lock.close()


@pytest.mark.parametrize('port', [None, 17, 'COM16', True])
def test_wrong_or_missing_legacy_instrument_never_prompts(guard_module, env, tmp_path, port):
    _, report = legacy(guard_module, tmp_path / 'plugins/esi/logs/esi_heater_characterization')
    data = json.loads(report.read_text())
    data['esi_com'] = port
    guard_module.save_json(report, data)
    with pytest.raises(RuntimeError, match='identity'):
        env().authorize_restart(input_fn=lambda _: pytest.fail('No declaration with uncertain identity'))


def test_archive_changed_after_consent_blocks_claim(guard_module, env, tmp_path):
    marker, _ = legacy(guard_module, tmp_path / 'plugins/esi/logs/esi_heater_characterization')
    original = marker.read_bytes()
    g = env()
    record = g.authorize_restart(input_fn=agree)[0]
    (Path(record['archive']) / 'runs/000/samples.csv').write_text('changed')
    d, r, _ = initial(guard_module, g)
    with pytest.raises(RuntimeError, match='archive evidence'):
        g.claim(d, r)
    assert marker.read_bytes() == original and not g.registry.exists()


@pytest.mark.parametrize('step', [1, 2, 3])
@pytest.mark.parametrize('after', [False, True])
def test_every_publication_interruption_prevents_construction(guard_module, env, monkeypatch, step, after):
    g = env(kind='pressure_temperature')
    g.authorize_restart()
    d, r, _ = initial(guard_module, g)
    save, calls = guard_module.save_json, []
    def interrupt(path, data):
        calls.append(path)
        if len(calls) == step:
            if after:
                save(path, data)
            raise KeyboardInterrupt('publication interrupted')
        return save(path, data)
    monkeypatch.setattr(guard_module, 'save_json', interrupt)
    with pytest.raises(KeyboardInterrupt):
        g.claim(d, r)
    assert not g._hardware_started and not g._claim_complete
    with pytest.raises(RuntimeError):
        g.mark_hardware_started()
    data = guard_module.read_json(r)
    data.update(shutdown_confirmed=True, pressure_closed=True)
    save(r, data)
    with pytest.raises(RuntimeError):
        g.finalize(shutdown_confirmed=True, owners_idle=True, auxiliary_closed=True, transport_poisoned=False)
    assert g.abandon_before_hardware() is False
    assert not g._released
    if step > 1 or after:
        assert g.registry.exists()


def test_release_interruption_keeps_central_claim(guard_module, env, monkeypatch):
    g = env(kind='pressure_temperature')
    g.authorize_restart()
    d, r, data = initial(guard_module, g)
    g.claim(d, r)
    g.mark_hardware_started()
    unlink, calls = Path.unlink, []
    def interrupt(path, *args, **kwargs):
        if str(path) in g._claim['marker_paths']:
            calls.append(path)
            if len(calls) == 2:
                raise OSError('release interrupted')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', interrupt)
    with pytest.raises(OSError, match='release interrupted'):
        complete(guard_module, g, r, data)
    assert g.registry.exists() and not g._released
    g._release_unused()  # Simulated process exit after failure; marker remains blocking.
    original_report = r.read_bytes()
    fresh = env(kind='pressure_temperature')
    with pytest.raises(RuntimeError, match='declaration'):
        fresh.authorize_restart(input_fn=lambda _: 'no')
    records = fresh.authorize_restart(input_fn=agree)
    assert records and r.read_bytes() == original_report
    assert guard_module.read_json(r)['shutdown_confirmed'] is True
    assert any(name.endswith('.missing.json') for name in records[0]['evidence_sha256'])
    d2, r2, data2 = initial(guard_module, fresh, 'restarted')
    fresh.claim(d2, r2)
    assert all(Path(p).exists() for p in fresh._claim['marker_paths'])
    assert complete(guard_module, fresh, r2, data2)
    assert r.read_bytes() == original_report


@pytest.mark.parametrize('damage', [None, 'report', 'csv', 'com', 'registry'])
def test_interrupted_claim_recovery_requires_intact_evidence(guard_module, env, monkeypatch, damage):
    g = env(kind='pressure_temperature')
    g.authorize_restart()
    d, r, data = initial(guard_module, g)
    save = guard_module.save_json
    def fail_shadow(path, value):
        if Path(path).name == guard_module.HEATER_MARKER:
            raise OSError('shadow write failed')
        return save(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(guard_module, 'save_json', fail_shadow)
        with pytest.raises(OSError, match='shadow'):
            g.claim(d, r)
    original_report = r.read_bytes()
    g._release_unused()  # TEST ONLY: emulate old process exit, no constructor was entered.
    if damage == 'report':
        r.unlink()
    elif damage == 'csv':
        (d / 'samples.csv').unlink()
    elif damage == 'com':
        data['esi_com'] = 17
        save(r, data)
    elif damage == 'registry':
        g.registry.write_text('{}')
    if damage in ('com', 'registry'):
        with pytest.raises(RuntimeError):
            fresh = env(kind='pressure_temperature')
            fresh.authorize_restart(input_fn=lambda _: pytest.fail('Invalid evidence must refuse before prompt'))
        return
    if damage:  # deleted report or samples: recorded, recovery still needs the declaration
        fresh = env(kind='pressure_temperature')
        record = fresh.authorize_restart(input_fn=agree)[0]
        assert record['missing_evidence'] and record['shutdown_confirmed'] is False
        return
    fresh = env(kind='pressure_temperature')
    records = fresh.authorize_restart(input_fn=agree)
    assert records and r.read_bytes() == original_report
    assert guard_module.read_json(r)['shutdown_confirmed'] is False
    d2, r2, _ = initial(guard_module, fresh, 'restarted')
    fresh.claim(d2, r2)
    assert all(Path(p).exists() for p in fresh._claim['marker_paths'])
    assert r.read_bytes() == original_report


@pytest.mark.parametrize('change_at', ['consent', 'after_archive'])
def test_missing_shadow_evidence_is_bound_to_operator_consent(guard_module, env, change_at):
    old = env(kind='pressure_temperature')
    old.authorize_restart()
    d, r, _ = initial(guard_module, old)
    old.claim(d, r)
    missing = Path(old._claim['marker_paths'][0])
    missing.unlink()  # TEST ONLY: interruption at a compatibility transition.
    old._release_unused()
    fresh = env(kind='pressure_temperature')
    if change_at == 'consent':
        def restore_during_prompt(prompt):
            guard_module.save_json(missing, old._claim)
            return agree(prompt)
        with pytest.raises(RuntimeError, match='changed'):
            fresh.authorize_restart(input_fn=restore_during_prompt)
    else:
        records = fresh.authorize_restart(input_fn=agree)
        assert not missing.exists()  # Authorization alone never repairs a shadow.
        name = next(k for k in records[0]['evidence_sha256'] if k.endswith('.missing.json'))
        (Path(records[0]['archive']) / name).write_text('{}')
        d2, r2, _ = initial(guard_module, fresh, 'restart')
        with pytest.raises(RuntimeError, match='archive evidence changed'):
            fresh.claim(d2, r2)
        assert not missing.exists()


def test_success_arguments_cannot_mask_failed_metadata_fsync(guard_module, env, monkeypatch):
    g = env()
    g.authorize_restart()
    d, r, data = initial(guard_module, g)
    g.claim(d, r)
    data['shutdown_confirmed'] = True
    guard_module.save_json(r, data)
    monkeypatch.setattr(guard_module, 'sync_file', lambda _: (_ for _ in ()).throw(OSError('fsync failed')))
    with pytest.raises(OSError, match='fsync'):
        g.finalize(shutdown_confirmed=True, owners_idle=True, auxiliary_closed=True, transport_poisoned=False)
    assert g.registry.exists() and not g._released


def test_clean_preconstruction_abandon_is_distinct_from_uncertain_restart(guard_module, env, tmp_path):
    g = env()
    g.authorize_restart()
    d, r, _ = initial(guard_module, g)
    g.claim(d, r)
    assert g.abandon_before_hardware() is True
    assert not g.registry.exists()
    legacy(guard_module, g.heater_dir)
    new = env(output_dir=tmp_path / 'other')
    new.authorize_restart(input_fn=agree)
    d, r, _ = initial(guard_module, new)
    new.claim(d, r)
    assert new.abandon_before_hardware() is False
    assert new.registry.exists() and not new._released
