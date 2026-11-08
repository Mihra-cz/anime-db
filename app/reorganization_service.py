"""Small public, derived service-page snapshot. Never execution authority.

Only confirmed E7/E1 notifications enter this observer. It performs no DB/media
lookups, retains no action history, and throttles local atomic publications.
"""
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time

from . import target_execution_filesystem as fs
from .tools.execution_manifest import validate_local_output

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 5
PHASES = ('manifest', 'folders', 'rename', 'sqlite', 'cleanup', 'verification')


class ServiceProgress:
    def __init__(self, directory, *, library_root=None, clock=time.time, monotonic=time.monotonic):
        self.directory = Path(directory).absolute()
        validate_local_output(self.directory / 'progress.json', *((library_root,) if library_root else ()))
        with fs._held_evidence_fd(fs._open_absolute(self.directory)) as fd:
            fs._secure_directory(fd)
        self._clock, self._monotonic = clock, monotonic
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread = None
        self._last_write = float('-inf')
        self._rows = {key: {'id': key, 'state': 'WORK' if key == 'manifest' else 'WAIT'} for key in PHASES}
        self._data = dict(version=1, run_id=secrets.token_hex(16), active=True,
            current_phase='manifest', completed=0, rename_completed=0, total=None,
            waiting_for_operator=False, outcome='UNVERIFIED', error=None,
            last_confirmed=None, phases=list(self._rows.values()))

    def _publish(self, *, required=False):
        temporary = None
        try:
            payload = {**self._data, 'updated_at': self._clock()}
            raw = json.dumps(payload, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
            fd, temporary = tempfile.mkstemp(prefix='.progress-', dir=self.directory)
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o644)  # Only this non-sensitive public summary is shared.
                stream.write(raw)
            # No fsync: this disposable derived view never grants execution or
            # restart authority. Atomic replace prevents partial JSON being served.
            os.replace(temporary, self.directory / 'progress.json')
            temporary = None
            self._last_write = self._monotonic()
        except OSError:
            if required:
                raise
            logger.warning('Service progress publication unavailable; execution evidence remains authoritative')
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def __enter__(self):
        with self._lock:
            self._publish(required=True)
        self._thread = threading.Thread(target=self._heartbeat, name='reorganization-service-snapshot', daemon=True)
        self._thread.start()
        return self

    def _heartbeat(self):
        while not self._stop.wait(INTERVAL_SECONDS):
            with self._lock:
                if self._data['active']:
                    self._publish()

    def _row(self, phase, state):
        changed = self._rows[phase]['state'] != state
        self._rows[phase]['state'] = state
        return changed

    def _confirmed(self, phase):
        self._data['last_confirmed'] = dict(phase=phase, state=self._rows[phase]['state'],
            completed=self._data['completed'], rename_completed=self._data['rename_completed'], at=self._clock())

    def observe(self, event):
        with self._lock:
            if not self._data['active']:
                return
            phase = event['phase']
            self._data['total'] = event['total']
            self._data['completed'] = event['completed']
            changed = False
            if phase == 'MANIFEST_CONFIRMED':
                changed = self._row('manifest', 'PASS')
                self._confirmed('manifest')
            elif phase == 'FOLDER_TREE_PREPARING':
                self._data['current_phase'] = 'folders'
                changed = self._row('folders', 'WORK')
            elif phase == 'FOLDER_TREE_CONFIRMED':
                changed = self._row('folders', 'PASS')
                self._confirmed('folders')
                self._data['current_phase'] = 'rename'
                changed = self._row('rename', 'WORK') or changed
            elif phase == 'FS_VERIFIED':
                self._data['rename_completed'] = max(self._data['rename_completed'], event['action_index'] + 1)
                self._confirmed('rename')
            elif phase == 'DB_RECONCILED':
                changed = self._row('sqlite', 'WORK')
                self._confirmed('sqlite')
            elif phase == 'COMPLETE':
                self._confirmed('sqlite')
            elif phase == 'ACTIONS_CONFIRMED':
                changed = self._row('rename', 'PASS')
                changed = self._row('sqlite', 'PASS') or changed
                self._confirmed('sqlite')
                self._data['current_phase'] = 'cleanup'
            elif phase == 'WAITING_FOR_CLEANUP_APPROVAL':
                self._data['current_phase'] = 'cleanup'
                self._data['waiting_for_operator'] = True
                changed = True
            elif phase == 'E7H_CLEANUP':
                self._data['waiting_for_operator'] = False
                changed = self._row('cleanup', 'WORK')
            elif phase == 'CLEANUP_CONFIRMED':
                changed = self._row('cleanup', 'PASS')
                self._confirmed('cleanup')
            elif phase in ('FINAL_VERIFICATION', 'NORMAL_VERIFICATION'):
                self._data['current_phase'] = 'verification'
                changed = self._row('verification', 'WORK')
            # PENDING/FS_APPLIED are attempts/incomplete work, never counts/PASS.
            if changed or self._monotonic() - self._last_write >= INTERVAL_SECONDS:
                self._publish()

    def finish(self, outcome, *, error=None):
        with self._lock:
            self._data['active'] = False
            self._data['waiting_for_operator'] = False
            self._data['outcome'] = outcome
            if outcome == 'NORMAL':
                # The original worker returned only after verification/NORMAL and
                # aggregate result publication. This is final positive evidence.
                for phase in PHASES:
                    self._row(phase, 'PASS')
                self._data['current_phase'] = 'verification'
                self._confirmed('verification')
            else:
                current = self._data['current_phase']
                for phase in PHASES:
                    if self._rows[phase]['state'] == 'WORK' or phase == current:
                        self._row(phase, 'STOP')
                code = type(error).__name__ if error is not None else 'ExecutionStopped'
                self._data['error'] = dict(code=code, message='Běh byl zastaven. Podrobnosti ověřte v terminálu správce.')
            self._publish()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def __exit__(self, error_type, error, traceback):
        if error is not None and self._data['active']:
            self.finish('STOP', error=error)
        self.close()
