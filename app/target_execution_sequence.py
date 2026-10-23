"""Shared live execution lifecycle for E6A and E7A; scratch authority stays in adapters."""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
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
from .target_execution_database import _ScratchSequenceDatabase
from .target_execution_directories import _DirectoryPreparation
from .target_execution_identity import (
    ScratchIdentitySession, FileIdentityEvidence, IdentityEvidenceReconciliationRequired, _file_stat, _hash_file,
)
from .target_execution_manifest import ExecutionManifest, JournalEntry, manifest_to_json
from .target_execution_rollback import _owner as _require_scratch_maintenance_owner


class _ExecutionSequence:
    """One lifecycle implementation; adapters provide fresh authority and DB stages."""

    def _require_journal(self):
        try:
            entries = self._writer.inspect_live(self._manifest)
            if (manifest_to_json(self._manifest) != self._manifest_bytes
                    or entries != tuple(self._history)):
                raise journal.ReconciliationRequired('STOP: unexpected live execution history/binding')
            return entries
        except Exception as exc:
            raise ScratchJournalReconciliationRequired('STOP: live execution journal unconfirmed', self._phase) from exc

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
        self._check_execution()
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
            raise ScratchJournalReconciliationRequired('STOP: execution journal append unacknowledged', state) from exc
        for index in self._captures:
            self._identity_check(index)

    def _run_move(self, index):
        action = self._manifest.payload.actions[index]
        self._check_execution()
        self._phase = 'VALIDATION'
        observation = self._resources.enter_context(self._filesystem.observe(self._manifest, action))
        self._observations[index] = observation
        self._check_prepared(index, observation)
        self._phase = 'EVIDENCE_CAPTURE'
        context = self._identities[index]
        captured = context.capture(self._manifest, action, action_index=index)
        self._phase = 'EVIDENCE_VERIFY'
        if (type(captured) is not FileIdentityEvidence or captured.action_index != index
                or captured.action != action or captured.filesystem != _file_stat(observation._before)):
            raise ScratchFilesystemReconciliationRequired('STOP: execution capture/action/index mismatch', self._phase)
        fd, parent = context._artifact_fd, context._artifact_parent_fd
        self._captures[index] = captured, fd, parent, _file_stat(context._check_artifact(fd, parent))
        self._identity_check(index, moved=False)
        if index == 0:
            self._phase = 'JOURNAL_CREATE'
            try:
                self._writer = self._resources.enter_context(journal.DurableJournal.create(self.journal_path, self._manifest))
                self._require_journal()
            except Exception as exc:
                raise ScratchJournalReconciliationRequired('STOP: fresh execution journal unavailable', self._phase) from exc
        else:
            self._require_predecessor()
        self._append('PENDING')
        self._phase = 'PRE_RENAME_IDENTITY'
        self._identity_check(index, moved=False)
        self._phase = 'RENAME'
        result = self._filesystem.rename(self._manifest, action, observation=observation)
        expected = ('KEEP', False, False) if action.action == 'KEEP' else ('DURABLE', True, True)
        if (type(result) is not fs.RenameResult or result.manifest_id != self._manifest.manifest_id
                or result.action_id != action.action_id
                or (result.outcome, result.syscall_succeeded, result.durability_confirmed) != expected):
            raise ScratchFilesystemReconciliationRequired('STOP: execution rename durability unconfirmed', self._phase)
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
            raise ScratchFilesystemReconciliationRequired('STOP: execution physical binding mismatch', self._phase)
        self._verifications[index] = verification
        self._phase = 'IDENTITY_VERIFICATION'
        self._identity_check(index, moved=True)
        self._append('FS_VERIFIED')
        self._phase = 'DB_PRE_UPDATE'
        self._require_journal()
        for previous in range(index + 1):
            self._verify_physical(previous)
        self._phase = 'DB_UPDATE'
        self._check_execution()
        self._database._apply(index)
        self._phase = 'DB_COMMIT'
        self._check_execution()
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
        return self._outcome(index, verification)

    def _outcome(self, index, verification):
        action = self._manifest.payload.actions[index]
        return CompletedScratchVideoOutcome(self._manifest.manifest_id, action.action_id, index,
            self.journal_path, self.database_path, action.object_id, action.target,
            action.target.split('/')[0], verification)

    def _finish(self, primary):
        self._closed = True
        self._authorized_b = None
        self._authorized_index = None
        try:
            self._resources.__exit__(type(primary) if primary else None,
                primary, primary.__traceback__ if primary else None)
        except BaseException as exc:
            if primary is not None and (isinstance(primary, (KeyboardInterrupt, SystemExit))
                    or not isinstance(exc, (KeyboardInterrupt, SystemExit))):
                primary.add_note(f'Execution STOP: cleanup failed: {type(exc).__name__}: {exc}')
                return
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._stop_class, self._phase = ScratchFilesystemReconciliationRequired, 'CLOSE'
            error = self._stop_class('STOP: execution resource cleanup failed', 'CLOSE')
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
            raise ScratchCoordinationStopped('STOP: execution owner unavailable', 'VALIDATION')
        return self

    def __exit__(self, _type, primary, _traceback):
        if os.getpid() != self._pid:
            raise ScratchCoordinationStopped('STOP: inherited owner has no SQLite cleanup authority', 'VALIDATION')
        with self._mutex:
            if not self._closed:
                self._finish(primary)

    def _check_execution(self):
        raise NotImplementedError('execution adapter must provide its authority boundary')

    def _check_prepared(self, index, observation):
        pass

    def _execute_once(self, manifest: ExecutionManifest, **scope):
        if os.getpid() != self._pid:
            raise ScratchCoordinationStopped('STOP: inherited execution owner', 'VALIDATION')
        with self._mutex:
            if self._spent or self._closed:
                error = self._stop_class('STOP: execution owner spent/closed; no retry or resume', self._phase)
                error.action_index = self._active_index
                raise error
            self._spent = True
            self._execution_thread = threading.get_ident()
            primary = None
            try:
                self._accept_scope(**scope)
                self._prepare_execution(manifest)
                return self._execute_actions()
            except BaseException as exc:
                primary = exc
                if isinstance(exc, ScratchCoordinationStopped):
                    if self._phase == 'P2C_COMPLETION' and not exc.reconciliation_required:
                        error = ScratchFilesystemReconciliationRequired('STOP: ' + str(exc), self._phase)
                    else:
                        error = exc
                elif isinstance(exc, Exception):
                    if self._phase == 'P2C_COMPLETION':
                        category = ScratchFilesystemReconciliationRequired
                    elif self._phase.startswith('DB_'):
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
                    exc.add_note(f'Execution STOP: action_index={self._active_index}; phase={self._phase}; no retry/resume')
                    self._stop_class = ScratchFilesystemReconciliationRequired
                    raise
                error.action_index = self._active_index
                self._stop_class, self._phase, primary = type(error), error.phase, error
                if error is exc:
                    raise
                raise error from exc
            finally:
                self._finish(primary)

    def _accept_scope(self):
        pass


class _ManifestFilesystem(fs.ScratchRenameSession):
    def _validate(self, manifest, action):
        fs._validate_scratch_binding(manifest, action, self.root, self.mount_identity)
        self._sequence_owner._authorize(manifest, action)

    def _keep_observation_authorized(self, manifest, action):
        self._validate(manifest, action)
        return True


@dataclass(frozen=True)
class CompletedManifestAction:
    manifest_id: str
    action_id: str
    action_index: int
    journal_path: Path
    database_path: Path
    action: str
    object_kind: str
    object_id: int | str
    verification: fs.ScratchVerification
    outcome: str = 'COMPLETE'


class ScratchManifestExecution(_ExecutionSequence):
    """Fresh scratch capability over the same lifecycle used by the E6A adapter.

    A caller explicitly submits the original approved complete manifest and a
    live exclusive E6B owner. No root/DB adoption, production switch or resume.
    """

    def __init__(self):
        raise TypeError('use ScratchManifestExecution.create; no existing root adoption')

    @classmethod
    def create(cls, parent: Path):
        resources = ExitStack()
        try:
            filesystem = resources.enter_context(_ManifestFilesystem.create(parent))
            owner = object.__new__(cls)
            owner._filesystem, owner._resources = filesystem, resources
            owner._mutex, owner._pid = threading.Lock(), os.getpid()
            owner._execution_thread = None
            owner._spent = owner._closed = False
            owner._manifest = owner._writer = owner._database = owner._maintenance_owner = None
            owner._authorized_index = None
            owner._identities, owner._guards = (), ()
            owner._history, owner._captures, owner._observations, owner._verifications = [], {}, {}, {}
            owner._prepared = {}
            owner._directories = None
            owner._cleanup_decision = owner.cleanup_outcome = None
            owner._completion_rescan_requested = False
            owner.completion_rescan_result = None
            owner._active_index, owner._phase = 0, 'VALIDATION'
            owner._stop_class = ScratchCoordinationStopped
            filesystem._sequence_owner = owner
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

    @property
    def directory_evidence_path(self):
        return self.root.parent / (self.root.name + '.directories.jsonl')

    @property
    def cleanup_evidence_path(self):
        return self.root.parent / (self.root.name + '.cleanup.jsonl')

    def execute(self, manifest: ExecutionManifest, *, maintenance_owner=None, cleanup_decision=None,
                completion_rescan=False):
        try:
            return self._execute_once(manifest, maintenance_owner=maintenance_owner,
                cleanup_decision=cleanup_decision, completion_rescan=completion_rescan)
        except BaseException:
            self.completion_rescan_result = None
            raise

    def _accept_scope(self, *, maintenance_owner, cleanup_decision=None, completion_rescan=False):
        if type(completion_rescan) is not bool or (completion_rescan and cleanup_decision is not None):
            raise ValueError('completion rescan requires an explicit bool and no cleanup decision')
        self._completion_rescan_requested = completion_rescan
        if cleanup_decision is not None and not callable(cleanup_decision):
            raise ValueError('cleanup_decision must explicitly decide a verified cleanup proposal')
        self._cleanup_decision = cleanup_decision
        self._maintenance_owner = maintenance_owner
        self._check_execution()

    def _check_execution(self):
        if (self._closed or not self._spent or os.getpid() != self._pid
                or not self._mutex.locked() or threading.get_ident() != self._execution_thread):
            raise fs.RenameRejected('STOP: no live sequential execution owner')
        # Reuse only the rollback's ownership validation, never its approval,
        # existing-root adoption or physical-return authority.
        _require_scratch_maintenance_owner(self._maintenance_owner, self.root)
        if self._directories is not None:
            self._directories.check_bindings()

    def _authorize(self, manifest, action):
        self._check_execution()
        if manifest is not self._manifest or action not in manifest.payload.actions:
            raise fs.RenameRejected('STOP: no exact active action authorization')
        index = manifest.payload.actions.index(action)
        # As in E6A, this capability is issued only after the live E1/DB/FS
        # predecessor gate. Boundary checks inspect the actual E1 writer;
        # nested read-only identity checks consume its acknowledged prefix.
        completed = {entry.action_id for entry in self._history if entry.state == 'COMPLETE'}
        if not ((index == self._active_index and self._authorized_index == index)
                or (index < self._active_index and action.action_id in completed)):
            raise fs.RenameRejected('STOP: action is neither active nor durably completed')
        if any(dependency not in completed for dependency in action.depends_on):
            raise fs.RenameRejected('STOP: dependency completion unconfirmed')

    def _identity_authorized(self, context, manifest, action, index):
        self._authorize(manifest, action)
        return self._identities[index] is context

    def _prepare_execution(self, manifest):
        if (type(manifest) is not ExecutionManifest or not manifest.payload.actions
                or manifest.payload.expected_post_hash is None):
            raise ValueError('nonempty sealed manifest and final post-state hash required')
        seen, objects, sources, targets = set(), set(), set(), set()
        # Only executable kinds emitted by the current planner. Full fresh-plan
        # parity below supplies the authority for each exact disposition/ID.
        file_actions = {
            'video': {'MOVE', 'KEEP', 'QUARANTINE'},
            'subtitle': {'MOVE', 'KEEP', 'QUARANTINE'},
            'confirmed_no_match': {'MOVE', 'KEEP'},
            'subtitle_archive': {'MOVE', 'KEEP'},
            'auxiliary': {'MOVE', 'KEEP'},
            'duplicate_side_asset': {'QUARANTINE', 'KEEP'},
            'keep_root': {'KEEP'},
        }
        for action in manifest.payload.actions:
            fs._validate_scratch_binding(manifest, action, self.root, self.mount_identity)
            key = action.object_kind, action.object_id
            database_object = action.object_kind in {'video', 'subtitle', 'confirmed_no_match'}
            valid_id = (type(action.object_id) is int and action.object_id > 0) if database_object else (
                type(action.object_id) is str and action.object_id == action.source)
            if (action.criticality not in {'PRIMARY', 'LOW'}
                    or action.action not in file_actions.get(action.object_kind, ()) or not valid_id
                    or key in objects or action.source in sources or action.target in targets
                    or len(set(action.depends_on)) != len(action.depends_on)
                    or any(dependency not in seen for dependency in action.depends_on)):
                raise ValueError('unsupported/duplicate action or non-topological sealed dependency order')
            seen.add(action.action_id)
            objects.add(key)
            sources.add(action.source)
            targets.add(action.target)
        if any(a.action != 'KEEP' and a.target in sources for a in manifest.payload.actions):
            raise ValueError('occupied-source chains require unsupported staging')
        self._manifest, self._manifest_bytes = manifest, manifest_to_json(manifest)
        self._phase = 'DB_VALIDATION'
        self._database = self._resources.enter_context(closing(
            _ScratchSequenceDatabase(self._filesystem, self.database_path, manifest)))
        self._phase = 'PREFLIGHT'
        self._directories = _DirectoryPreparation(self, self._database._initial_snapshot)
        # Read-only pins of all initial sources, without granting observation or
        # rename authority to actions whose dependencies are not yet COMPLETE.
        for index, action in enumerate(manifest.payload.actions):
            with ExitStack() as paths:
                source_parent = self._filesystem._parent(fs._locator(action.source), paths)
                fd = os.open(fs._locator(action.source)[-1], fs._FILE, dir_fd=source_parent)
                self._resources.enter_context(fs._held_evidence_fd(fd))
                self._filesystem._check_fd(fd)
                before = fs._check_source(fd, source_parent, fs._locator(action.source)[-1], action)
                observed, content = _hash_file(fd)
                if action.action != 'KEEP':
                    self._directories.target_absent(action)
                self._prepared[index] = fd, before, observed, content
        self._check_execution()
        for index, action in enumerate(manifest.payload.actions):
            self._verify_prepared(index)
        self._directories.prepare()
        self._identities = tuple(self._resources.enter_context(
            ScratchIdentitySession._new_execution_context(self._filesystem, self, index))
            for index in range(len(manifest.payload.actions)))
        self._guards = tuple(_LiveIdentityChecks(context) for context in self._identities)

    def _verify_prepared(self, index):
        action = self._manifest.payload.actions[index]
        fd, before, observed, content = self._prepared[index]
        self._filesystem._check_root()
        with ExitStack() as paths:
            parent = self._filesystem._parent(fs._locator(action.source), paths)
            fs._check_source(fd, parent, fs._locator(action.source)[-1], action, before)
            if _hash_file(fd) != (observed, content):
                raise fs.RenameRejected('STOP: preflight source changed')
            if action.action != 'KEEP':
                self._directories.target_absent(action)

    def _check_prepared(self, index, observation):
        self._verify_prepared(index)
        if _file_stat(observation._before) != self._prepared[index][2]:
            raise fs.RenameRejected('STOP: observation no longer matches the original preflight pin')

    def _require_predecessor(self):
        self._phase = 'DEPENDENCY_GATE'
        entries = self._require_journal()
        expected = tuple(JournalEntry(self._manifest.manifest_id, action.action_id, index, state)
            for index, action in enumerate(self._manifest.payload.actions[:self._active_index])
            for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'))
        if entries != expected:
            raise ScratchJournalReconciliationRequired('STOP: incomplete acknowledged manifest prefix', self._phase)
        action = self._manifest.payload.actions[self._active_index]
        journal._validate_entry(self._manifest, list(entries),
            {e.action_id for e in entries if e.state == 'COMPLETE'},
            JournalEntry(self._manifest.manifest_id, action.action_id, self._active_index, 'PENDING'))
        self._phase = 'DB_PREDECESSOR'
        self._database._verify_stored(self._active_index - 1)
        self._phase = 'PREDECESSOR_IDENTITY'
        for index in range(self._active_index):
            self._verify_physical(index)
        self._require_journal()

    def _execute_actions(self):
        result = []
        for index in range(len(self._manifest.payload.actions)):
            self._active_index = index
            if index:
                self._require_predecessor()
            self._authorized_index = index
            result.append(self._run_move(index))
        if self._completion_rescan_requested:
            from .scanner.execution_completion import run_completed_scratch_rescan
            self._phase = 'P2C_COMPLETION'
            self.completion_rescan_result = run_completed_scratch_rescan(self)
        if self._cleanup_decision is not None:
            from .target_execution_cleanup import _run_cleanup
            self.cleanup_outcome = _run_cleanup(self, self._cleanup_decision)
        return tuple(result)

    def _identity_check(self, index, *, moved=None):
        action = self._manifest.payload.actions[index]
        if action.action != 'KEEP' or moved is None:
            return super()._identity_check(index, moved=moved)
        captured, fd, parent, before = self._captures[index]
        self._guards[index]._check_identity_anchor(self._manifest, action, captured, fd, parent, before, self._phase)
        comparison = self._identities[index].compare(self._manifest, action, action_index=index)
        if (comparison.outcome != 'MATCH' or comparison.source != comparison.target
                or comparison.source is None or comparison.source.presence != 'PRESENT'
                or comparison.source.observed_file is None
                or comparison.source.observed_file.ctime_ns != captured.filesystem.ctime_ns
                or comparison.source.physical_identity_match != 'MATCH'
                or comparison.source.filesystem_match != 'MATCH' or comparison.source.content_match != 'MATCH'):
            raise ScratchFilesystemReconciliationRequired('STOP: KEEP live identity/content unconfirmed', self._phase)
        self._guards[index]._check_identity_anchor(self._manifest, action, captured, fd, parent, before, self._phase)

    def _outcome(self, index, verification):
        action = self._manifest.payload.actions[index]
        return CompletedManifestAction(self._manifest.manifest_id, action.action_id, index, self.journal_path,
            self.database_path, action.action, action.object_kind, action.object_id, verification)
