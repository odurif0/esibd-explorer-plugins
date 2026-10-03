"""Bounded DMMR receive recovery, shared by the plugin and notebook.

Never reconnect or retry a blocked DLL call. Configuration writes are verified,
not replayed; shutdown may repeat OFF commands after one resynchronization.
"""
from __future__ import annotations

from collections import deque
import time

RECEIVE_ERRORS = frozenset((-10, -11, -12, -13))


class ReceiveError(RuntimeError):
    def __init__(self, action, status):
        self.action, self.status = action, status
        super().__init__(f"{action}: receive status {status}")


class _RecoveryCalls:
    def __init__(self, device, *, timeout_s, clock=time.monotonic,
                 continue_check=lambda: True):
        self.device = device
        self.timeout_s, self.clock, self.continue_check = timeout_s, clock, continue_check
        self.last_incident = None

    def _active(self):
        if getattr(self.device, '_transport_poisoned', False):
            raise RuntimeError('DLL blocked; no recovery or concurrent calls permitted.')
        if getattr(self.device, 'connected', True) is False:
            raise RuntimeError('Port disconnected; no automatic reconnection permitted.')
        if not self.continue_check():
            raise RuntimeError('Operation cancelled; recovery stopped.')

    def _record(self, event):
        recorder = getattr(self.device, 'record_protocol_event', None)
        if callable(recorder):
            recorder(event)

    def _call(self, name, *args, raw=False):
        self._active()
        device = self.device
        if raw:
            result = device._call_locked_with_timeout(
                getattr(device, name), self.timeout_s,
                f'{getattr(self, "phase", "read")}_recovery_{name}', *args)
        else:
            result = getattr(device, name)(*args, timeout_s=self.timeout_s)
        self._active()  # A late return must not authorize another call.
        status = result[0] if isinstance(result, tuple) else result
        self.last_incident['operations'].append({'action': name, 'arguments': args, 'status': status})
        if status in RECEIVE_ERRORS:
            raise ReceiveError(name, status)
        if status != device.NO_ERR:
            raise RuntimeError(f'{name}: status {status}')
        return result[1:] if isinstance(result, tuple) else ()

    def _resynchronize(self):
        incident = self.last_incident
        incident['purge_attempted'] = True
        incident['data_loss'] = 'purge attempted; pending serial data may be lost'
        self._call('purge', raw=True)
        incident['purged'] = True
        incident['data_loss'] = 'pending serial data discarded; lost sample count unknown'
        # The bundled CGC purge resets the host and controller to 9600 baud.
        (baud,) = self._call('set_baud_rate', self.device.baudrate, raw=True)
        if baud != self.device.baudrate:
            raise RuntimeError('Baud rate not restored after purge.')


class CommandRecovery(_RecoveryCalls):
    """One recovery budget per startup (or per separate OFF attempt).

    verify must read the requested hardware state, without replaying a write.
    Only shutdown uses purge_first and may repeat the two OFF commands.
    """
    def __init__(self, device, *, phase, **kwargs):
        super().__init__(device, **kwargs)
        self.phase = phase
        self.used = False

    def verify_running(self):
        state, _label = self._call('get_state')
        state = int(state, 16) if isinstance(state, str) else int(state)
        if state != 0:
            raise RuntimeError(f'Controller state is not ST_ON: {state}')
        (enabled,) = self._call('get_enable')
        if enabled is not True:
            raise RuntimeError('Measurement enable not confirmed; no automatic re-enable.')
        (automatic,) = self._call('get_automatic_current')
        if automatic is not False:
            raise RuntimeError('Manual current acquisition mode not confirmed.')
        return self.device.NO_ERR, state, _label

    def recover(self, status, action, verify, *, purge_first=False):
        if status not in RECEIVE_ERRORS:
            raise RuntimeError(f'{action}: non-recoverable status {status}')
        incident = self.last_incident = {
            'kind': 'command_recovery', 'phase': self.phase, 'action': action,
            'status': int(status), 'verified': False, 'purged': False,
            'purge_attempted': False, 'operations': [],
            'started_monotonic_s': self.clock(),
        }
        try:
            self._active()
            if self.used:
                raise RuntimeError('Command recovery budget exhausted.')
            self.used = True
            if purge_first:
                self._resynchronize()
                result = verify()
            else:
                try:
                    result = verify()
                except ReceiveError:
                    self._resynchronize()
                    result = verify()
            self._active()
            incident['verified'] = True
            return result
        except BaseException as exc:
            incident['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            incident['finished_monotonic_s'] = self.clock()
            self._record(dict(incident))


class ReadRecovery(_RecoveryCalls):
    """One verification, at most one purge, then require fresh samples.

    A second fault before all modules deliver a valid sample is persistent.
    Three incidents per rolling minute are tolerated, not an endless retry loop.
    The failed measurement is never returned or reconstructed by this class.
    """
    MAX_RECOVERIES = 3
    WINDOW_S = 60.0

    def __init__(self, device, ranges, *, automatic, timeout_s,
                 clock=time.monotonic, continue_check=lambda: True):
        super().__init__(device, timeout_s=timeout_s, clock=clock, continue_check=continue_check)
        self.ranges = dict(ranges)  # address -> (observed range, auto-range flag)
        if not self.ranges or any(
            type(a) is not int or a not in range(8)
            or type(r) is not int or r not in range(5) or type(auto) is not bool
            for a, (r, auto) in self.ranges.items()
        ):
            raise ValueError("Recovery requires verified module ranges.")
        self.automatic = bool(automatic)
        self.recent = deque()
        self.awaiting_samples = set()
        self.recovery_count = 0
        self.last_incident = None
        self.failed = False

    def _verify(self, *, automatic):
        state, _label = self._call('get_state')
        state = int(state, 16) if isinstance(state, str) else int(state)
        if state != 0:
            raise RuntimeError(f'Controller state is not ST_ON: {state}')
        (enabled,) = self._call('get_enable')
        if enabled is not True:
            raise RuntimeError('Measurement enable not confirmed; no automatic re-enable.')
        (actual_auto,) = self._call('get_automatic_current')
        if actual_auto is not automatic:
            raise RuntimeError('Current acquisition mode changed; recovery stopped.')
        for address, (requested, auto) in self.ranges.items():
            actual, actual_auto = self._call('get_module_meas_range', address)
            if (type(actual) is not int or actual not in range(5)
                    or actual_auto is not auto or (not auto and actual != requested)):
                raise RuntimeError(f'Module {address}: measurement range not confirmed.')

    def recover(self, status, action):
        """Recover a returned error; exceptions, bad states and ranges are fatal."""
        if status not in RECEIVE_ERRORS:
            raise RuntimeError(f'{action}: non-recoverable status {status}')
        now = self.clock()
        while self.recent and now - self.recent[0] >= self.WINDOW_S:
            self.recent.popleft()
        incident = self.last_incident = {
            'kind': 'read_recovery', 'action': action, 'status': int(status),
            'started_monotonic_s': now, 'verified': False, 'resumed': False,
            'purged': False, 'purge_attempted': False, 'discarded_frames': 0, 'operations': [],
            'data_loss': ('failed read excluded; no reconstruction' if self.automatic
                          else 'failed polling cycle excluded; no reconstruction'),
        }
        try:
            self._active()
            if self.failed or self.awaiting_samples or len(self.recent) >= self.MAX_RECOVERIES:
                raise RuntimeError('Persistent/repeated receive errors; recovery budget exhausted.')
            self.recent.append(now)
            self.recovery_count += 1
            try:
                # A lost response can leave the next transaction healthy. Do not
                # interrupt the stream or purge good queued data unnecessarily.
                self._verify(automatic=self.automatic)
            except ReceiveError:
                # Returned framing error, not a blocked call or a bad readback.
                # Purge resets the baud rate to 9600 in the bundled CGC DLL.
                self._resynchronize()
                self._call('set_automatic_current', False)
                self._verify(automatic=False)
                # Drain the remaining DLL FIFO before starting a new segment.
                # Keep this bounded even if the device ignores automatic OFF.
                flush_deadline = self.clock() + self.timeout_s
                for _ in range(1024):
                    self._active()
                    remaining = flush_deadline - self.clock()
                    if remaining <= 0:
                        raise TimeoutError('FIFO did not empty within the read timeout.')
                    result = self.device.get_current(timeout_s=remaining)
                    self._active()
                    if result[0] == self.device.NO_DATA:
                        break
                    if result[0] != self.device.NO_ERR:
                        raise RuntimeError(f'FIFO flush failed: status {result[0]}')
                    incident['discarded_frames'] += 1
                else:
                    raise RuntimeError('FIFO did not empty after automatic OFF.')
                if self.automatic:
                    self._call('set_automatic_current', True)
                    (enabled,) = self._call('get_automatic_current')
                    if enabled is not True:
                        raise RuntimeError('Automatic current restart not confirmed.')
            self._active()
            incident['verified'] = True
            self.awaiting_samples = set(self.ranges)
            return incident
        except BaseException as exc:
            self.failed = True
            incident['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            incident['finished_monotonic_s'] = self.clock()
            self._record(dict(incident))

    def note_sample(self, address):
        """Call only for a new valid sample, never for a repeated/stale frame."""
        if not self.awaiting_samples:
            return
        self.awaiting_samples.discard(address)
        if not self.awaiting_samples:
            self.last_incident['resumed'] = True
            self._record({'kind': 'read_resumed', 'action': self.last_incident['action'],
                          'status': self.last_incident['status'],
                          'monotonic_s': self.clock(), 'modules': sorted(self.ranges)})
