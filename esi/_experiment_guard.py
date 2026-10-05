"""Shared, fail-closed notebook ownership/recovery. No instrument or runtime I/O."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4

HEATER_MARKER = '.heater_characterization.running.json'
PT_MARKER = '.pressure_temperature.running.json'


def utc():
    return datetime.now(timezone.utc).isoformat()


def sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def durable_new_file(path, contents):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError('Linked evidence/guard is not accepted')
    with path.open('xb') as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def save_json(path, data):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Linked evidence/guard is not accepted')
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.tmp')
    durable_new_file(temporary, json.dumps(data, indent=2, allow_nan=False).encode())
    os.replace(temporary, path)
    sync_directory(path.parent)


def regular_bytes(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f'Missing or non-regular evidence: {path}')
    with path.open('rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeError(f'Non-regular evidence: {path}')
        return stream.read()


def sync_file(path):
    if Path(path).is_symlink():
        raise RuntimeError('Linked evidence is not accepted')
    with Path(path).open('r+b') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeError('Non-regular evidence')
        os.fsync(stream.fileno())


def read_json(path):
    try:
        value = json.loads(regular_bytes(path))
    except (ValueError, OSError) as error:
        raise RuntimeError(f'Invalid evidence: {path}') from error
    if not isinstance(value, dict):
        raise RuntimeError(f'Invalid evidence object: {path}')
    return value


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError('Timezone missing')
        return result
    except (TypeError, ValueError) as error:
        raise RuntimeError('Unfinished run: invalid timestamp') from error


class RunLock:
    """OS-held lock. Never unlink the shared inode, including after release."""
    def __init__(self, directory, name='.heater_characterization.lock'):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / name
        self.stream = None
        try:
            if self.path.is_symlink():
                raise RuntimeError('Ownership lock must be a regular file')
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            self.stream = os.fdopen(descriptor, 'r+b')
            if not stat.S_ISREG(os.fstat(self.stream.fileno()).st_mode):
                raise RuntimeError('Ownership lock must be a regular file')
            if not os.fstat(self.stream.fileno()).st_size:
                self.stream.write(b'0')
                self.stream.flush()
                os.fsync(self.stream.fileno())
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.check()
        except BaseException:
            if self.stream is not None:
                self.stream.close()
            raise RuntimeError('Another owner/recovery holds this instrument, or file locking failed')

    def check(self):
        if self.stream is None or self.stream.closed or self.path.is_symlink():
            raise RuntimeError('Run ownership lost')
        opened, current = os.fstat(self.stream.fileno()), self.path.stat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError('Ownership lock changed')

    def close(self):
        self.stream.close()


def require_fresh_kernel(root, kernel_globals):
    if any(kernel_globals.get(name) for name in ('_HC_GUARD', '_PT_GUARD')):
        raise RuntimeError('Unfinished run in this kernel: terminate its process; recovery here is forbidden')
    runtime = root / 'vendor/runtime'
    for name, module in tuple(sys.modules.items()):
        if name.startswith('_esibd_bundled_'):
            raise RuntimeError('Preloaded instrument namespace: use a fresh kernel')
        source = getattr(module, '__file__', None)
        if source:
            try:
                inside = Path(source).resolve().is_relative_to(runtime)
            except (OSError, ValueError, TypeError):
                inside = False
            if inside:
                raise RuntimeError('Preloaded ESI runtime: use a fresh kernel')


def _absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise RuntimeError('Unfinished run: invalid registered path')
    return Path(value)


def _validate_port(report, com):
    ports = [report[key] for key in ('esi_com', 'esi_port') if key in report]
    if not ports or any(type(port) is not int or port != com for port in ports):
        raise RuntimeError('Unfinished run: report instrument identity is missing or differs')


class MissingEvidence(RuntimeError):
    """The original run evidence no longer exists (e.g. a deleted run folder).

    Recovery then still requires the explicit operator declaration; the absence
    is archived with it instead of leaving no software recovery path at all.
    """


def _evidence(marker, data, com):
    """Resolve one marker unambiguously; never choose the newest report."""
    stamp = timestamp(data.get('started_utc'))
    if data.get('guard_version') == 1:
        directory = _absolute(data.get('run_directory'))
        report_path = _absolute(data.get('report_path'))
        if report_path.parent != directory or report_path.name not in ('report.json', 'metadata.json'):
            raise RuntimeError('Unfinished run: invalid registered report')
        if directory.is_symlink():
            raise RuntimeError('Unfinished run: linked run directory')
        if not report_path.exists() and not report_path.is_symlink():
            raise MissingEvidence(f'registered report {report_path} no longer exists')
        report = read_json(report_path)
        if timestamp(report.get('started_utc')) != stamp:
            raise RuntimeError('Unfinished run: registered report identity changed')
    else:
        named = data.get('run_directory')
        if named is not None:
            if not isinstance(named, str) or Path(named).name != named or named in ('.', '..') or '\\' in named:
                raise RuntimeError('Unfinished run: invalid original run directory')
            candidates = [marker.parent / named]
        else:
            candidates = [p for p in marker.parent.iterdir() if p.is_dir()]
        matched = []
        report_name = 'report.json' if marker.name == HEATER_MARKER else 'metadata.json'
        if named is not None and not (candidates[0] / report_name).exists() and not candidates[0].is_symlink():
            raise MissingEvidence(f'original report {candidates[0] / report_name} no longer exists')
        for candidate in candidates:
            report_path = candidate / report_name
            if candidate.is_symlink() or not report_path.is_file():
                continue
            report = read_json(report_path)
            try:
                start = timestamp(report.get('started_utc'))
                end = timestamp(report.get('ended_utc', report.get('started_utc')))
                matches = start == stamp if named else start <= stamp <= end
            except RuntimeError:
                matches = False
            if matches:
                matched.append((candidate, report_path, report))
        if not matched:
            raise MissingEvidence(f'no original report in {marker.parent} matches this marker')
        if len(matched) != 1:
            raise RuntimeError('Unfinished run: require exactly one matching original report')
        directory, report_path, report = matched[0]
    _validate_port(report, com)
    confirmation = report.get('shutdown_confirmed', 'missing')
    if confirmation is not False:
        # PT can leave a guard despite verified ESI shutdown: a pressure owner/close
        # may still be uncertain. Preserve that distinction, never rewrite True.
        pt_uncertain = report_path.name == 'metadata.json' and (
            confirmation is None or (confirmation is True and report.get('outcome') == 'shutdown_unconfirmed'))
        registered_shutdown = data.get('guard_version') == 1 and confirmation is True
        if not (pt_uncertain or registered_shutdown):
            raise RuntimeError('Unfinished run: missing or contradictory shutdown report')
    if not (directory / 'samples.csv').exists() and not (directory / 'samples.csv').is_symlink():
        raise MissingEvidence(f'samples {directory / "samples.csv"} no longer exist')
    regular_bytes(directory / 'samples.csv')
    files = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Unfinished run: linked evidence is not accepted')
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = regular_bytes(path)
    if report_path.name not in files or 'samples.csv' not in files:
        raise RuntimeError('Unfinished run: incomplete evidence')
    return directory, report_path, files


class ExperimentGuard:
    """One instrument lease, one claim, explicit operator recovery, no device I/O.

    Per-user COM identity is not protection against other accounts, physical port
    aliases or old programs which do not implement this protocol.
    """
    def __init__(self, *, plugin_dir, com, kind, output_dir, kernel_globals,
                 legacy_pt_dirs=(), state_root=None):
        if type(com) is not int or not 1 <= com <= 255:
            raise ValueError('ESI COM must be an integer in 1..255')
        if kind not in ('heater', 'pressure_temperature'):
            raise ValueError('Unknown ESI experiment kind')
        if not isinstance(kernel_globals, dict):
            raise TypeError('Pass the notebook globals for the fresh-kernel check')
        self.root = Path(plugin_dir).expanduser().resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.com, self.kind = com, kind
        self.port = f'COM{com}'
        state_root = Path.home() / '.esibd/esi_experiment_guards' if state_root is None else Path(state_root)
        self.state_dir = state_root.expanduser().resolve() / f'port-{self.port}'
        self.registry = self.state_dir / 'active.json'
        self.heater_dir = self.root / 'logs/esi_heater_characterization'
        self._paths = {self.registry, self.heater_dir / HEATER_MARKER,
                       self.root.parent / 'notebooks/pressure_temperature_runs' / PT_MARKER}
        self._paths.update(Path(p).expanduser().resolve() / PT_MARKER for p in legacy_pt_dirs)
        if kind == 'pressure_temperature':
            self._paths.add(self.output_dir / PT_MARKER)
        self._locks = []
        self._authorized = False
        self._kernel_globals = kernel_globals
        self._claim_complete = False
        self._records = []
        self._snapshot_before = None
        self._claim = None
        self._transition_started = False
        self._hardware_started = False
        self._released = False
        require_fresh_kernel(self.root, kernel_globals)
        try:
            # Always COM first, then the compatibility lock: fixed ordering.
            self._locks.append(RunLock(self.state_dir, '.owner.lock'))
            discovered = self._discover()
            directories = {self.heater_dir}
            directories.update(path.parent for path in discovered if path.name == HEATER_MARKER)
            for directory in sorted(directories, key=str):
                self._locks.append(RunLock(directory))
            again = self._discover()
            if any(path.name == HEATER_MARKER and path.parent not in directories for path in again):
                raise RuntimeError('Historical ownership changed while acquiring locks')
        except BaseException:
            self._release_unused()
            raise

    def _release_unused(self):
        for lock in reversed(self._locks):
            lock.close()
        self._released = True

    def _check_locks(self):
        if self._released:
            raise RuntimeError('Run ownership was released')
        for lock in self._locks:
            lock.check()

    def _discover(self):
        paths, visited, result = set(self._paths), set(), {}
        central, central_registered, missing = None, set(), set()
        if self.registry.exists() or self.registry.is_symlink():
            central = read_json(self.registry)
            if (central.get('guard_version') != 1 or type(central.get('com')) is not int
                    or central['com'] != self.com or not isinstance(central.get('marker_paths'), list)):
                raise RuntimeError('Unfinished run: invalid central registry identity')
            central_registered = {_absolute(name) for name in central['marker_paths']}
        while paths - visited:
            if len(paths) > 64:
                raise RuntimeError('Unfinished run: invalid marker graph')
            path = sorted(paths - visited, key=str)[0]
            visited.add(path)
            if not path.exists() and not path.is_symlink():
                continue
            raw = regular_bytes(path)
            data = read_json(path)
            if data.get('guard_version') == 1:
                if data.get('com') != self.com or type(data.get('com')) is not int:
                    raise RuntimeError('Unfinished run: registered COM identity differs')
                if not isinstance(data.get('claim_id'), str) or not data['claim_id']:
                    raise RuntimeError('Unfinished run: invalid claim identity')
                registered = data.get('marker_paths')
                if not isinstance(registered, list):
                    raise RuntimeError('Unfinished run: missing registered markers')
                for name in registered:
                    linked = _absolute(name)
                    if linked.name not in (HEATER_MARKER, PT_MARKER):
                        raise RuntimeError('Unfinished run: invalid registered marker name')
                    if linked.is_symlink():
                        raise RuntimeError('Unfinished run: linked registered evidence')
                    if not linked.exists():
                        if central is None or linked not in central_registered:
                            raise RuntimeError('Unfinished run: registered evidence missing without central claim')
                        missing.add(linked)
                    paths.add(linked)
            result[path] = (raw, data)
        for path in missing:
            if path.exists() or path.is_symlink() or read_json(self.registry) != central:
                raise RuntimeError('Unfinished run: evidence changed during discovery')
            result[path] = (None, central)
        return result

    def _snapshot(self):
        markers = self._discover()
        files, sources = {}, {}
        runs = {}
        missing = []
        for index, (path, (raw, data)) in enumerate(sorted(markers.items(), key=lambda item: str(item[0]))):
            if raw is None:
                # Record absence, not fabricated marker bytes. The central claim
                # and its original report/CSV are still mandatory evidence.
                archive_name = f'markers/{index:03d}.missing.json'
                files[archive_name] = json.dumps({'missing_registered_shadow': str(path),
                                                 'registry': str(self.registry)}, sort_keys=True).encode()
                sources[archive_name] = str(path)
                continue
            archive_name = f'markers/{index:03d}.json'
            files[archive_name], sources[archive_name] = raw, str(path)
            try:
                directory, report_path, contents = _evidence(path, data, self.com)
            except MissingEvidence as absence:
                # Record the absence; never fabricate the missing report or samples.
                name = f'missing/{index:03d}.json'
                files[name] = json.dumps({'marker': str(path), 'missing': str(absence)}, sort_keys=True).encode()
                sources[name] = str(path)
                missing.append(str(absence))
                continue
            key = str(report_path)
            if key not in runs:
                prefix = f'runs/{len(runs):03d}/'
                runs[key] = str(directory)
                for name, value in contents.items():
                    files[prefix + name] = value
                    sources[prefix + name] = str(directory / name)
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
        return {'markers': markers, 'files': files, 'sources': sources, 'hashes': hashes, 'missing': missing}

    def _unchanged(self, evidence):
        current = self._snapshot()
        return current['hashes'] == evidence['hashes'] and current['sources'] == evidence['sources']

    def _verify_archives(self):
        for record in self._records:
            archive = Path(record['archive'])
            if read_json(archive / 'operator-declaration.json') != record:
                raise RuntimeError('Operator archive declaration changed')
            for name, digest in record['evidence_sha256'].items():
                if hashlib.sha256(regular_bytes(archive / name)).hexdigest() != digest:
                    raise RuntimeError('Operator archive evidence changed')

    def authorize_restart(self, input_fn=input):
        self._check_locks()
        require_fresh_kernel(self.root, self._kernel_globals)
        if self._authorized or self._transition_started:
            raise RuntimeError('Restart authorization already used')
        evidence = self._snapshot()
        if evidence['markers']:
            identity = hashlib.sha256(json.dumps({'sources': evidence['sources'], 'hashes': evidence['hashes']},
                                                sort_keys=True).encode()).hexdigest()
            phrase = 'RESTART ' + identity[:12]
            print('Previous run cleanup remains unresolved; original shutdown results are preserved.')
            for absence in evidence['missing']:
                print(f'Original evidence missing (recorded, not reconstructed): {absence}.')
            print('Restarting a kernel is not hardware OFF.')
            print(f'Operator declaration ONLY: equipment physically safe AND all former {self.port}/pressure-owning processes terminated.')
            if input_fn('Declare BOTH conditions and authorize one restart: ' + phrase + ': ') != phrase:
                raise RuntimeError('Unfinished run: explicit run-specific operator declaration not supplied')
            self._check_locks()
            if not self._unchanged(evidence):
                raise RuntimeError('Original evidence changed during confirmation')
            archive = self.state_dir / 'operator_restarts' / (identity[:12] + '_' + uuid4().hex)
            archive.mkdir(parents=True, exist_ok=False)
            for name, contents in evidence['files'].items():
                durable_new_file(archive / name, contents)
                if regular_bytes(archive / name) != contents:
                    raise RuntimeError('Restart archive verification failed')
            record = {'acknowledged_utc': utc(), 'kind': 'operator_authorized_restart',
                      'physical_safety_declared': True, 'previous_process_termination_declared': True,
                      'process_termination_verified_by_software': False, 'shutdown_confirmed': False,
                      'evidence_sha256': evidence['hashes'], 'original_paths': evidence['sources'],
                      'missing_evidence': evidence['missing'],
                      'archive': str(archive), 'instrument': self.port}
            save_json(archive / 'operator-declaration.json', record)
            for folder in sorted((p for p in archive.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                sync_directory(folder)
            sync_directory(archive)
            sync_directory(archive.parent)
            sync_directory(self.state_dir)
            if not self._unchanged(evidence):
                raise RuntimeError('Original evidence changed while archiving')
            self._records = [record]
        self._snapshot_before = evidence
        self._authorized = True
        return json.loads(json.dumps(self._records))

    def claim(self, run_directory, report_path):
        self._check_locks()
        if not self._authorized or self._transition_started:
            raise RuntimeError('Authorize/discover first; one claim per owner')
        directory, report_path = Path(run_directory), Path(report_path)
        if directory.is_symlink() or report_path.is_symlink():
            raise RuntimeError('Linked run evidence is not accepted')
        directory, report_path = directory.resolve(), report_path.resolve()
        if directory.parent != self.output_dir or report_path.parent != directory:
            raise RuntimeError('Run/report directory does not match this owner')
        expected_name = 'report.json' if self.kind == 'heater' else 'metadata.json'
        if report_path.name != expected_name:
            raise RuntimeError('Unexpected report filename')
        report = read_json(report_path)
        _validate_port(report, self.com)
        timestamp(report.get('started_utc'))
        if report.get('shutdown_confirmed') is not False or report.get('operator_restarts', []) != self._records:
            raise RuntimeError('Initial report must retain shutdown uncertainty and recovery evidence')
        regular_bytes(directory / 'samples.csv')
        sync_file(report_path)
        sync_file(directory / 'samples.csv')
        sync_directory(directory)
        sync_directory(directory.parent)
        if not self._unchanged(self._snapshot_before):
            raise RuntimeError('Original evidence changed before transition')
        self._verify_archives()
        markers = set(self._snapshot_before['markers']) - {self.registry}
        markers.add(self.heater_dir / HEATER_MARKER)
        if self.kind == 'pressure_temperature':
            markers.add(self.output_dir / PT_MARKER)
        self._claim = {'guard_version': 1, 'claim_id': uuid4().hex, 'com': self.com, 'kind': self.kind,
                       'plugin_dir': str(self.root), 'run_directory': str(directory), 'report_path': str(report_path),
                       'started_utc': report['started_utc'], 'shutdown_confirmed': False,
                       'operator_restarts': self._records, 'marker_paths': sorted(map(str, markers)),
                       'warning': 'Unfinished run: operator restart is not verified shutdown.'}
        self._transition_started = True
        # Central claim goes first. Any interruption leaves a blocking record.
        save_json(self.registry, self._claim)
        for marker in sorted(markers, key=str):
            save_json(marker, self._claim)
        self.check()
        self._claim_complete = True
        return json.loads(json.dumps(self._claim))

    def check(self):
        self._check_locks()
        if self._claim is not None:
            for path in [self.registry, *map(Path, self._claim['marker_paths'])]:
                if read_json(path) != self._claim:
                    raise RuntimeError('Run claim changed; no transition permitted')

    def mark_hardware_started(self):
        if self._claim is None or not self._claim_complete or self._hardware_started:
            raise RuntimeError('Complete claim first; hardware-start boundary is one-shot')
        self.check()
        self._hardware_started = True

    def finalize(self, *, shutdown_confirmed, owners_idle, auxiliary_closed, transport_poisoned):
        self.check()
        if self._claim is None or not self._claim_complete:
            raise RuntimeError('No completely claimed run to finalize')
        if not (shutdown_confirmed is True and owners_idle is True and auxiliary_closed is True
                and transport_poisoned is False):
            return False
        report = read_json(self._claim['report_path'])
        _validate_port(report, self.com)
        if (report.get('shutdown_confirmed') is not True
                or report.get('started_utc') != self._claim['started_utc']
                or (self.kind == 'pressure_temperature' and report.get('pressure_closed') is not True)):
            raise RuntimeError('Final report does not confirm shutdown; uncertainty retained')
        sync_file(self._claim['report_path'])
        sync_directory(Path(self._claim['report_path']).parent)
        return self._clear_claim()

    def _clear_claim(self):
        self.check()
        # Marker removal is not a multi-file transaction. Keep the central record
        # until every compatibility marker has been durably removed.
        for path in map(Path, self._claim['marker_paths']):
            if read_json(path) != self._claim:
                raise RuntimeError('Run marker changed before release')
            path.unlink()
            sync_directory(path.parent)
        if read_json(self.registry) != self._claim:
            raise RuntimeError('Central claim changed before release')
        self.registry.unlink()
        sync_directory(self.registry.parent)
        self._release_unused()
        return True

    def abandon_before_hardware(self):
        if self._hardware_started:
            raise RuntimeError('Cannot abandon after a constructor may have accessed hardware')
        if self._transition_started:
            # Only a complete, fresh claim with no inherited uncertainty can be
            # abandoned on structural proof that no constructor was entered.
            if self._claim_complete and not self._records:
                return self._clear_claim()
            return False
        self._release_unused()
        return True
