"""Private E6A: exactly two fresh scratch Video MOVEs, never recovery authority.

No public primitive dependency waiver, arbitrary DB path, adoption or N-action
executor. The sole owner retains every observation, identity pin and E1 writer.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
import os
from pathlib import Path
import threading

from . import target_execution_filesystem as fs
from . import target_execution_journal as journal
from .target_execution_coordination import (
    _LiveIdentityChecks, CompletedScratchVideoOutcome, ScratchCoordinationStopped,
    ScratchDatabaseReconciliationRequired, ScratchFilesystemReconciliationRequired,
    ScratchJournalReconciliationRequired,
)
from .target_execution_database import _ScratchPairDatabase
from .target_execution_identity import (
    ScratchIdentitySession, FileIdentityEvidence, IdentityEvidenceReconciliationRequired, _file_stat,
)
from .target_execution_manifest import ExecutionManifest, JournalEntry, manifest_to_json


class _PairFilesystem(fs.ScratchRenameSession):
    def _validate(self, manifest, action):
        fs._validate_scratch_binding(manifest, action, self.root, self.mount_identity)
        owner = self._pair_owner
        if (os.getpid() != owner._pid or not owner._spent or owner._closed
                or not owner._mutex.locked() or threading.get_ident() != owner._execution_thread
                or manifest != owner._manifest
                or (action.depends_on and not owner._dependency_authorized(manifest, action))):
            raise fs.RenameRejected('STOP: no live pair-owner authorization')


class _ScratchVideoMovePair:
    """Factory-only, single-use owner of precisely A then dependent B."""

    def __init__(self):
        raise TypeError('use _ScratchVideoMovePair.create; no existing-context adoption')

    @classmethod
    def create(cls, parent: Path):
        resources = ExitStack()
        try:
            filesystem = resources.enter_context(_PairFilesystem.create(parent))
            owner = object.__new__(cls)
            owner._filesystem = filesystem
            owner._resources = resources
            owner._identities = tuple(resources.enter_context(ScratchIdentitySession._new_context(
                filesystem, owns_filesystem=False, bound_index=i)) for i in (0, 1))
            owner._guards = tuple(_LiveIdentityChecks(context) for context in owner._identities)
            owner._mutex, owner._pid = threading.Lock(), os.getpid()
            owner._execution_thread = None
            owner._spent = owner._closed = False
            owner._manifest = owner._authorized_b = owner._writer = owner._database = None
            owner._history, owner._captures, owner._observations, owner._verifications = [], {}, {}, {}
            owner._active_index, owner._phase = 0, 'VALIDATION'
            owner._stop_class = ScratchCoordinationStopped
            filesystem._pair_owner = owner
            return owner
        except BaseException as exc:
            resources.__exit__(type(exc), exc, exc.__traceback__)
            raise

    @property
    def root(self):
        return self._filesystem.root

    @property
    def mount_identity(self):
        return self._filesystem.mount_identity

    @property
    def journal_path(self):
        return self.root.parent / (self.root.name + '.journal.jsonl')

    @property
    def database_path(self):
        return self.root.parent / (self.root.name + '.sqlite')

    @property
    def evidence_paths(self):
        return tuple(context.evidence_path for context in self._identities)

    def _dependency_authorized(self, manifest, action):
        return (os.getpid() == self._pid and self._spent and not self._closed
            and self._mutex.locked() and self._active_index == 1
            and self._authorized_b is self._manifest and manifest == self._manifest
            and action == self._manifest.payload.actions[1]
            and action.depends_on == (self._manifest.payload.actions[0].action_id,)
            and self._writer is not None and not self._writer._closed)

    def _validate_pair(self, manifest):
        if type(manifest) is not ExecutionManifest or len(manifest.payload.actions) != 2:
            raise ValueError('exactly two sealed actions required')
        a, b = manifest.payload.actions
        if (a.depends_on or b.depends_on != (a.action_id,)
                or a.object_id == b.object_id
                or any(action.action != 'MOVE' or action.object_kind != 'video'
                    or action.criticality != 'PRIMARY' for action in (a, b))
                or len({a.source, a.target, b.source, b.target}) != 4):
            raise ValueError('two distinct PRIMARY Video MOVEs in sealed A -> B order required')
        # Structural validation does not issue observation or rename authority for B.
        for action in (a, b):
            fs._validate_scratch_binding(manifest, action, self.root, self.mount_identity)
        self._manifest_bytes = manifest_to_json(manifest)

    def _require_journal(self):
        try:
            entries = self._writer.inspect_live(self._manifest)
            if (manifest_to_json(self._manifest) != self._manifest_bytes
                    or entries != tuple(self._history)):
                raise journal.ReconciliationRequired('STOP: unexpected live pair history/binding')
            return entries
        except Exception as exc:
            raise ScratchJournalReconciliationRequired('STOP: live pair journal unconfirmed', self._phase) from exc

    def _identity_check(self, index, *, moved=None):
        guard = self._guards[index]
        args = self._captures[index]
        if moved is None:
            guard._check_identity_anchor(self._manifest, self._manifest.payload.actions[index],
                *args, self._phase)
        else:
            guard._require_identity(self._manifest, self._manifest.payload.actions[index],
                *args, self._phase, moved=moved)

    def _verify_physical(self, index):
        try:
            verification = self._observations[index].verify()
        except ScratchCoordinationStopped:
            raise
        except Exception as exc:
            raise ScratchFilesystemReconciliationRequired('STOP: physical verification failed', self._phase) from exc
        if verification != self._verifications[index]:
            raise ScratchFilesystemReconciliationRequired('STOP: completed physical state changed', self._phase)
        self._identity_check(index, moved=True)

    def _append(self, state):
        self._phase = state
        for index in self._captures:
            self._identity_check(index)
            if index < self._active_index:
                self._verify_physical(index)
        self._require_journal()
        entry = JournalEntry(self._manifest.manifest_id,
            self._manifest.payload.actions[self._active_index].action_id, self._active_index, state)
        try:
            self._writer.append(entry)
            self._history.append(entry)
            self._require_journal()
        except Exception as exc:
            raise ScratchJournalReconciliationRequired('STOP: pair journal append unacknowledged', state) from exc
        for index in self._captures:
            self._identity_check(index)

    def _require_predecessor(self):
        self._phase = 'DEPENDENCY_GATE'
        entries = self._require_journal()
        expected = tuple(JournalEntry(self._manifest.manifest_id,
            self._manifest.payload.actions[0].action_id, 0, state)
            for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'))
        if entries != expected:
            raise ScratchJournalReconciliationRequired('STOP: durable COMPLETE A is missing', self._phase)
        candidate = JournalEntry(self._manifest.manifest_id, self._manifest.payload.actions[1].action_id, 1, 'PENDING')
        # Use the unchanged E1 dependency gate over freshly inspected acknowledged history.
        journal._validate_entry(self._manifest, list(entries),
            {entry.action_id for entry in entries if entry.state == 'COMPLETE'}, candidate)
        self._phase = 'DB_PREDECESSOR'
        self._database._verify_stored(0)
        self._phase = 'PREDECESSOR_IDENTITY'
        self._verify_physical(0)
        self._require_journal()

    def _run_move(self, index):
        action = self._manifest.payload.actions[index]
        self._phase = 'VALIDATION'
        observation = self._resources.enter_context(self._filesystem.observe(self._manifest, action))
        self._observations[index] = observation
        self._phase = 'EVIDENCE_CAPTURE'
        context = self._identities[index]
        captured = context.capture(self._manifest, action, action_index=index)
        self._phase = 'EVIDENCE_VERIFY'
        if (type(captured) is not FileIdentityEvidence or captured.action_index != index
                or captured.action != action or captured.filesystem != _file_stat(observation._before)):
            raise ScratchFilesystemReconciliationRequired('STOP: pair capture/action/index mismatch', self._phase)
        fd, parent = context._artifact_fd, context._artifact_parent_fd
        self._captures[index] = captured, fd, parent, _file_stat(context._check_artifact(fd, parent))
        self._identity_check(index, moved=False)
        if index == 0:
            self._phase = 'JOURNAL_CREATE'
            try:
                self._writer = self._resources.enter_context(journal.DurableJournal.create(self.journal_path, self._manifest))
                self._require_journal()
            except Exception as exc:
                raise ScratchJournalReconciliationRequired('STOP: fresh pair journal unavailable', self._phase) from exc
        else:
            self._require_predecessor()
        self._append('PENDING')
        self._phase = 'PRE_RENAME_IDENTITY'
        self._identity_check(index, moved=False)
        self._phase = 'RENAME'
        result = self._filesystem.rename(self._manifest, action, observation=observation)
        if (type(result) is not fs.RenameResult or result.manifest_id != self._manifest.manifest_id
                or result.action_id != action.action_id or result.outcome != 'DURABLE'
                or result.syscall_succeeded is not True or result.durability_confirmed is not True):
            raise ScratchFilesystemReconciliationRequired('STOP: pair rename durability unconfirmed', self._phase)
        self._append('FS_APPLIED')
        self._phase = 'VERIFICATION'
        try:
            verification = observation.verify()
        except ScratchCoordinationStopped:
            raise
        except Exception as exc:
            raise ScratchFilesystemReconciliationRequired('STOP: physical verification failed', self._phase) from exc
        if (type(verification) is not fs.ScratchVerification or verification.manifest_id != self._manifest.manifest_id
                or verification.action_id != action.action_id):
            raise ScratchFilesystemReconciliationRequired('STOP: pair physical binding mismatch', self._phase)
        self._verifications[index] = verification
        self._phase = 'IDENTITY_VERIFICATION'
        self._identity_check(index, moved=True)
        self._append('FS_VERIFIED')
        self._phase = 'DB_PRE_UPDATE'
        self._require_journal()
        for previous in range(index + 1):
            self._verify_physical(previous)
        self._phase = 'DB_UPDATE'
        self._database._apply(index)
        self._phase = 'DB_COMMIT'
        self._database._commit(index)
        for state in ('DB_RECONCILED', 'COMPLETE'):
            self._phase = 'DB_VERIFICATION'
            self._database._verify_stored(index)
            self._phase = 'IDENTITY_VERIFICATION'
            for previous in range(index + 1):
                self._verify_physical(previous)
            self._append(state)
        self._phase = 'DB_FINAL_VERIFICATION'
        self._database._verify_stored(index)
        self._phase = 'IDENTITY_VERIFICATION'
        for previous in range(index + 1):
            self._verify_physical(previous)
        self._require_journal()
        return CompletedScratchVideoOutcome(self._manifest.manifest_id, action.action_id, index,
            self.journal_path, self.database_path, action.object_id, action.target,
            action.target.split('/')[0], verification)

    def execute(self, manifest: ExecutionManifest):
        if os.getpid() != self._pid:
            raise ScratchCoordinationStopped('STOP: inherited pair owner', 'VALIDATION')
        with self._mutex:
            if self._spent or self._closed:
                error = self._stop_class('STOP: pair owner spent/closed; no retry or resume', self._phase)
                error.action_index = self._active_index
                raise error
            self._spent = True
            self._execution_thread = threading.get_ident()
            primary = None
            try:
                self._validate_pair(manifest)
                self._manifest = manifest
                self._phase = 'DB_VALIDATION'
                self._database = self._resources.enter_context(closing(
                    _ScratchPairDatabase(self._filesystem, self.database_path, manifest)))
                first = self._run_move(0)
                self._active_index = 1
                self._require_predecessor()
                # This capability belongs only to this active owner and exact envelope.
                self._authorized_b = self._manifest
                second = self._run_move(1)
                return first, second
            except BaseException as exc:
                primary = exc
                if isinstance(exc, ScratchCoordinationStopped):
                    error = exc
                elif isinstance(exc, Exception):
                    if self._phase.startswith('DB_'):
                        category = ScratchDatabaseReconciliationRequired
                    elif isinstance(exc, IdentityEvidenceReconciliationRequired):
                        category = ScratchFilesystemReconciliationRequired
                    elif isinstance(exc, fs.RenameRejected) and not isinstance(exc, fs.RenameReconciliationRequired):
                        category = ScratchCoordinationStopped
                    elif self._phase in {'VALIDATION', 'EVIDENCE_CAPTURE'}:
                        category = ScratchCoordinationStopped
                    else:
                        category = ScratchFilesystemReconciliationRequired
                    error = category('STOP: ' + str(exc), self._phase)
                else:
                    exc.add_note(f'E6A STOP: action_index={self._active_index}; phase={self._phase}; no retry/resume')
                    self._stop_class = ScratchFilesystemReconciliationRequired
                    raise
                error.action_index = self._active_index
                self._stop_class, self._phase, primary = type(error), error.phase, error
                if error is exc:
                    raise
                raise error from exc
            finally:
                self._finish(primary)

    def _finish(self, primary):
        self._closed = True
        self._authorized_b = None
        try:
            self._resources.__exit__(type(primary) if primary else None,
                primary, primary.__traceback__ if primary else None)
        except BaseException as exc:
            if primary is not None and (isinstance(primary, (KeyboardInterrupt, SystemExit))
                    or not isinstance(exc, (KeyboardInterrupt, SystemExit))):
                primary.add_note(f'E6A STOP: cleanup failed: {type(exc).__name__}: {exc}')
                return
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._stop_class, self._phase = ScratchFilesystemReconciliationRequired, 'CLOSE'
            error = self._stop_class('STOP: pair resource cleanup failed', 'CLOSE')
            error.action_index = self._active_index
            raise error from exc

    def close(self):
        if os.getpid() != self._pid:
            raise ScratchCoordinationStopped('STOP: inherited owner has no SQLite cleanup authority', 'VALIDATION')
        with self._mutex:
            if not self._closed:
                self._finish(None)

    def __enter__(self):
        if self._closed or os.getpid() != self._pid:
            raise ScratchCoordinationStopped('STOP: pair owner unavailable', 'VALIDATION')
        return self

    def __exit__(self, _type, primary, _traceback):
        if os.getpid() != self._pid:
            raise ScratchCoordinationStopped('STOP: inherited owner has no SQLite cleanup authority', 'VALIDATION')
        with self._mutex:
            if not self._closed:
                self._finish(primary)
