"""Shared live execution lifecycle for E6A and E7A; scratch authority stays in adapters."""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from functools import wraps
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
    ScratchIdentitySession, FileIdentityEvidence, IdentityEvidenceReconciliationRequired, _file_stat, _hash_file, _frame,
    _file_evidence,
)
from .target_execution_manifest import ExecutionManifest, JournalEntry, manifest_to_json
from .target_execution_rollback import _owner as _require_scratch_maintenance_owner


def _filesystem_checkpoint(check):
    """Coalesce nested E7 checks, never the whole action or execution."""
    @wraps(check)
    def checked(self, *args, **kwargs):
        with self._filesystem._checkpoint():
            return check(self, *args, **kwargs)
    return checked


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

    def _check_journal(self):
        self._require_journal()

    def _capture_indices(self):
        return self._captures

    def _verification_indices(self, index):
        return range(index + 1)

    def _identity_check(self, index, *, moved=None):
        guard = self._guards[index]
        args = self._captures[index]
        if moved is None:
            guard._check_identity_anchor(self._manifest, self._manifest.payload.actions[index],
                *args, self._phase)
        else:
            return guard._require_identity(self._manifest, self._manifest.payload.actions[index],
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
        for index in self._capture_indices():
            self._identity_check(index)
            if index < self._active_index:
                self._verify_physical(index)
        self._check_journal()
        entry = JournalEntry(self._manifest.manifest_id,
            self._manifest.payload.actions[self._active_index].action_id, self._active_index, state)
        try:
            self._writer.append(entry)
            self._history.append(entry)
            self._check_journal()
        except Exception as exc:
            raise ScratchJournalReconciliationRequired('STOP: execution journal append unacknowledged', state) from exc
        if state == 'COMPLETE' and hasattr(self, '_completed_actions'):
            self._completed_actions.add(entry.action_id)
        for index in self._capture_indices():
            self._identity_check(index)

    def _run_move(self, index):
        action = self._manifest.payload.actions[index]
        self._check_execution()
        self._phase = 'VALIDATION'
        observation = getattr(self, '_action_resources', self._resources).enter_context(
            self._filesystem.observe(self._manifest, action))
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
                self._check_journal()
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
        self._check_journal()
        for previous in self._verification_indices(index):
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
            for previous in self._verification_indices(index):
                self._verify_physical(previous)
            self._append(state)
        self._phase = 'DB_FINAL_VERIFICATION'
        self._database._verify_stored(index)
        self._phase = 'IDENTITY_VERIFICATION'
        for previous in self._verification_indices(index):
            self._verify_physical(previous)
        self._check_journal()
        return self._outcome(index, verification)

    def _outcome(self, index, verification):
        action = self._manifest.payload.actions[index]
        return CompletedScratchVideoOutcome(self._manifest.manifest_id, action.action_id, index,
            self.journal_path, self.database_path, action.object_id, action.target,
            action.target.split('/')[0], verification)

    def _finish(self, primary):
        guard = getattr(self, '_persistent_guard', None)
        if primary is not None and guard is not None:
            guard.stop_scanning(primary)
        self._closed = True
        self._authorized_b = None
        self._authorized_index = None
        try:
            self._resources.__exit__(type(primary) if primary else None,
                primary, primary.__traceback__ if primary else None)
        except BaseException as exc:
            if guard is not None:
                guard.stop_scanning(exc)
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
    @classmethod
    def open_existing(cls, root, *, share, share_root):
        """Read-only descriptor allocation; physical authority still comes from E7 admission."""
        root = fs._absolute(Path(root))
        if root in {Path('/'), Path('/tmp')}:
            raise fs.RenameRejected('explicit existing library root required')
        fd = fs._open_absolute(root)
        try:
            fs._secure_directory(fd)
            from . import target_execution_mounts as media
            mounts = fs._read_mounts()
            mount = media._check_production_mount(root, fd, mounts,
                share=share, share_root=share_root, writable=True)
            fs._load_renameat2()
            session = object.__new__(cls)
            session._root, session._root_fd = root, fd
            session._root_identity = fs._identity(os.fstat(fd))
            session._mounts, session._mount_id = mounts, mount.mount_id
            session._mount_identity = f'{mount.fstype}:{mount.source}:{session._root_identity[0]}:{session._root_identity[1]}'
            session._production_binding = {'share': share, 'share_root': share_root}
            session._pid, session._mutex = os.getpid(), threading.Lock()
            session._attempted, session._poisoned, session._closed = set(), False, False
            session._check_root()
            return session
        except BaseException as exc:
            fs._close_evidence_fd(fd, exc)
            raise

    def _validate(self, manifest, action):
        owner = self._sequence_owner
        if manifest is not owner._manifest or not getattr(owner, '_manifest_validated', False):
            raise fs.RenameRejected('STOP: no admitted immutable manifest')
        owner._authorize(manifest, action)

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
    """Scratch factory and explicit production binding over the same E7 lifecycle.

    A caller explicitly submits the original approved complete manifest and a
    live exclusive E6B owner. Existing production paths additionally require
    canonical approval, runtime preflight and completed cleanup review. No resume.
    """

    def __init__(self):
        raise TypeError('use create or explicit open_existing; no restart/resume adoption')

    @classmethod
    def create(cls, parent: Path, *, database_parent: Path | None = None):
        """Allocate fresh media and optionally independent local SQLite storage."""
        return cls._allocate(parent, database_parent=database_parent)

    @classmethod
    def open_existing(cls, root: Path, *, database_path: Path, share: str, share_root='/'):
        """Explicit existing-only production adapter over the same E7 lifecycle."""
        return cls._allocate(None, existing_root=root, database_path=database_path,
            share=share, share_root=share_root)

    @classmethod
    def _allocate(cls, parent, *, database_parent=None, existing_root=None, database_path=None,
                  share=None, share_root='/'):
        resources = ExitStack()
        try:
            filesystem = resources.enter_context(_ManifestFilesystem.create(parent) if existing_root is None else
                _ManifestFilesystem.open_existing(existing_root, share=share, share_root=share_root))
            owner = object.__new__(cls)
            owner._filesystem, owner._resources = filesystem, resources
            owner._production_binding = getattr(filesystem, '_production_binding', None)
            storage = fs._absolute(Path(database_parent)) if database_parent is not None else filesystem.root.parent
            if existing_root is not None:
                database_path = fs._absolute(Path(database_path))
                if not database_path.is_file() or database_path.is_relative_to(filesystem.root):
                    raise ValueError('existing local SQLite outside the media library required')
                storage = database_path.parent
            owner._database_path = database_path if existing_root is not None else storage / (filesystem.root.name + '.sqlite')
            from .tools.execution_manifest import validate_local_output
            validate_local_output(owner._database_path, filesystem.root)
            owner._storage_fd = resources.enter_context(fs._held_evidence_fd(fs._open_absolute(storage)))
            fs._secure_directory(owner._storage_fd)
            owner._storage_identity = fs._identity(os.fstat(owner._storage_fd))
            owner._storage_mount = filesystem._check_state_mount(storage, owner._storage_fd).mount_id
            owner._database_pin = None
            if existing_root is not None:
                from .target_execution_guard import _guard_path, _scratch_evidence_present, _present
                if _present(_guard_path(database_path)) or _scratch_evidence_present(database_path):
                    raise ValueError('STOP: execution evidence already exists; no adoption/retry')
                fd = os.open(database_path.name, fs._FILE, dir_fd=owner._storage_fd)
                resources.enter_context(fs._held_evidence_fd(fd))
                info = journal._check_file(fd, owner._storage_fd, database_path.name)
                header = os.pread(fd, 100, 0)
                if (len(header) != 100 or header[:16] != b'SQLite format 3\0' or header[18:20] != b'\x01\x01'
                        or int.from_bytes(header[60:64], 'big') != 9):
                    raise ValueError('STOP: existing standalone schema-v9 rollback-mode SQLite required')
                for suffix in ('-wal', '-shm', '-journal'):
                    fs._absent(owner._storage_fd, database_path.name + suffix)
                owner._database_pin = fd, fs._identity(info)
            owner._mutex, owner._pid = threading.Lock(), os.getpid()
            owner._execution_thread = None
            owner._spent = owner._closed = False
            owner._manifest = owner._writer = owner._database = owner._maintenance_owner = None
            owner._authorized_index = None
            owner._identities, owner._guards = (), ()
            owner._history, owner._captures, owner._observations, owner._verifications = [], {}, {}, {}
            owner._prepared = {}
            owner._completed_stats = {}
            owner._active_stats = {}
            owner._content_sources = frozenset()
            owner._completed_actions = set()
            owner._manifest_validated = False
            owner._directories = None
            owner._persistent_guard = None
            owner._cleanup_decision = owner.cleanup_outcome = None
            owner._completion_rescan_requested = False
            owner._resume_scanning_requested = False
            owner.completion_rescan_result = None
            owner._active_index, owner._phase = 0, 'VALIDATION'
            owner._stop_class = ScratchCoordinationStopped
            filesystem._sequence_owner = owner
            if database_parent is not None or existing_root is not None:
                from .maintenance import ScratchMaintenance
                # Persist the existing binding before any plain scanner can enter.
                ScratchMaintenance(owner.database_path, owner.root,
                    **({'production_share': share, 'share_root': share_root} if existing_root is not None else {}))
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
        return self.database_path.with_suffix('.journal.jsonl')

    @property
    def database_path(self):
        return self._database_path

    @property
    def evidence_paths(self):
        return tuple(context.evidence_path for context in self._identities)

    @property
    def directory_evidence_path(self):
        return self.database_path.with_suffix('.directories.jsonl')

    @property
    def cleanup_evidence_path(self):
        return self.database_path.with_suffix('.cleanup.jsonl')

    def _check_storage_fd(self, fd):
        """Evidence is local to SQLite, independently of the media mount."""
        self._filesystem._check_root()
        journal._check_parent(self.database_path, self._storage_fd)
        fs._secure_directory(self._storage_fd)
        if (fs._identity(os.fstat(self._storage_fd)) != self._storage_identity
                or self._filesystem._check_state_mount(self.database_path.parent, self._storage_fd).mount_id != self._storage_mount
                or os.fstat(fd).st_dev != self._storage_identity[0]
                or fs._mount_id(fd) != self._storage_mount):
            raise fs.RenameRejected('STOP: database/evidence storage binding changed')

    def execute(self, manifest: ExecutionManifest, *, maintenance_owner=None, cleanup_decision=None,
                completion_rescan=False, resume_scanning=False, runtime_evidence=None, approved_manifest_sha256=None,
                verify_content=(), progress=None):
        try:
            return self._execute_once(manifest, maintenance_owner=maintenance_owner,
                cleanup_decision=cleanup_decision, completion_rescan=completion_rescan,
                resume_scanning=resume_scanning, runtime_evidence=runtime_evidence,
                approved_manifest_sha256=approved_manifest_sha256, verify_content=verify_content, progress=progress)
        except BaseException:
            self.completion_rescan_result = None
            raise

    def _accept_scope(self, *, maintenance_owner, cleanup_decision=None, completion_rescan=False,
                      resume_scanning=False, runtime_evidence=None, approved_manifest_sha256=None, verify_content=(), progress=None):
        if progress is not None and not callable(progress):
            raise ValueError('progress must be a callable observer')
        self._progress = progress
        if (type(verify_content) is not tuple or any(type(path) is not str for path in verify_content)
                or len(set(verify_content)) != len(verify_content)):
            raise ValueError('explicit unique source paths required for optional content verification')
        self._content_sources = frozenset(verify_content)
        if type(resume_scanning) is not bool:
            raise ValueError('resume_scanning requires an explicit bool')
        self._resume_scanning_requested = resume_scanning
        if type(completion_rescan) is not bool or (completion_rescan and cleanup_decision is not None and not resume_scanning):
            raise ValueError('completion rescan requires an explicit bool and no cleanup decision')
        self._completion_rescan_requested = completion_rescan or resume_scanning
        if cleanup_decision is not None and not callable(cleanup_decision):
            raise ValueError('cleanup_decision must explicitly decide a verified cleanup proposal')
        self._cleanup_decision = cleanup_decision
        self._maintenance_owner = maintenance_owner
        self._runtime_evidence, self._approved_manifest_sha256 = runtime_evidence, approved_manifest_sha256
        if self._production_binding and (not resume_scanning or cleanup_decision is None):
            raise ValueError('production execution requires explicit cleanup review and completion to SCANNING')
        self._check_execution()

    @_filesystem_checkpoint
    def _check_execution(self):
        if (self._closed or not self._spent or os.getpid() != self._pid
                or not self._mutex.locked() or threading.get_ident() != self._execution_thread):
            raise fs.RenameRejected('STOP: no live sequential execution owner')
        # Reuse only the rollback's ownership validation, never its approval,
        # existing-root adoption or physical-return authority.
        _require_scratch_maintenance_owner(self._maintenance_owner, self.root, database_path=self.database_path)
        if self._production_binding:
            gate = self._maintenance_owner.boundary
            if (gate.production_share != self._production_binding['share']
                    or gate.share_root != self._production_binding['share_root']):
                raise fs.RenameRejected('STOP: production maintenance share binding differs')
            fd, identity = self._database_pin
            if (fs._identity(os.fstat(fd)) != identity
                    or fs._identity(self.database_path.lstat()) != identity):
                raise fs.RenameRejected('STOP: original production SQLite identity changed')
        if self._persistent_guard is not None:
            self._persistent_guard.check()
        if self._directories is not None:
            paths = set()
            if self._manifest is not None:
                action = self._manifest.payload.actions[self._active_index]
                for locator in (action.source, action.target):
                    parts = locator.split('/')[:-1]
                    paths.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
            self._directories.check_bindings(paths=paths)

    def _authorize(self, manifest, action):
        self._check_execution()
        if (manifest is not self._manifest or action.action_id not in self._action_indices
                or action != manifest.payload.actions[self._action_indices[action.action_id]]):
            raise fs.RenameRejected('STOP: no exact active action authorization')
        index = self._action_indices[action.action_id]
        # As in E6A, this capability is issued only after the live E1/DB/FS
        # predecessor gate. Boundary checks inspect the actual E1 writer;
        # nested read-only identity checks consume its acknowledged prefix.
        completed = self._completed_actions
        if not ((index == self._active_index and self._authorized_index == index)
                or (index < self._active_index and action.action_id in completed)):
            raise fs.RenameRejected('STOP: action is neither active nor durably completed')
        if any(dependency not in completed for dependency in action.depends_on):
            raise fs.RenameRejected('STOP: dependency completion unconfirmed')

    def _identity_authorized(self, context, manifest, action, index):
        self._authorize(manifest, action)
        return self._identities[index] is context

    def _metadata_rename(self, index):
        return self._manifest.payload.actions[index].source not in self._content_sources

    def _prepare_execution(self, manifest):
        if (type(manifest) is not ExecutionManifest or not manifest.payload.actions
                or manifest.payload.expected_post_hash is None):
            raise ValueError('nonempty sealed manifest and final post-state hash required')
        if self._production_binding:
            import hashlib
            from dataclasses import replace
            from .target_execution_manifest import RuntimeEvidence, preflight_execution_manifest
            from .tools.execution_manifest import _read_current, observe_runtime
            if (type(self._runtime_evidence) is not RuntimeEvidence or
                    hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest() != self._approved_manifest_sha256):
                raise ValueError('exact canonical manifest approval and runtime evidence required')
            context, snapshot, _, fingerprint, _ = _read_current(self.database_path, self.root, manifest.payload.windows_root)
            observed = observe_runtime(self.root)
            runtime = replace(self._runtime_evidence, mount_identity=self.mount_identity,
                mount_read_only_now=observed['mount_read_only_now'], free_bytes=observed['free_bytes'])
            result = preflight_execution_manifest(manifest, context, snapshot, fingerprint, runtime=runtime)
            if result.outcome != 'READY_FOR_EXECUTION':
                raise ValueError('production preflight ' + result.outcome + ': ' + ','.join(result.diagnostics))
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
        fs._validate_scratch_binding(manifest, manifest.payload.actions[0], self.root, self.mount_identity)
        for action in manifest.payload.actions:
            from .target_planner import import_namespace
            fs._locator(action.source)
            fs._locator(action.target)
            if import_namespace(action.source) or import_namespace(action.target):
                raise ValueError('protected IMPORT is outside reorganization authority')
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
        import hashlib
        self._manifest_sha256 = hashlib.sha256(self._manifest_bytes.encode()).hexdigest()
        self._action_indices = {action.action_id: index for index, action in enumerate(manifest.payload.actions)}
        self._manifest_validated = True
        if not self._content_sources <= sources:
            raise ValueError('optional content verification names an unapproved source')
        self._phase = 'DB_VALIDATION'
        self._database = self._resources.enter_context(closing(
            _ScratchSequenceDatabase(self._filesystem, self.database_path, manifest)))
        self._phase = 'PREFLIGHT'
        self._directories = _DirectoryPreparation(self, self._database._initial_snapshot)
        # Metadata admission never reads media payload. Only explicitly selected
        # sources receive FULL evidence. No library-sized FD set is retained.
        for index, action in enumerate(manifest.payload.actions):
            with ExitStack() as paths:
                source_parent = self._filesystem._parent(fs._locator(action.source), paths)
                fd = os.open(fs._locator(action.source)[-1], fs._FILE, dir_fd=source_parent)
                paths.enter_context(fs._held_evidence_fd(fd))
                self._filesystem._check_fd(fd)
                before = fs._check_source(fd, source_parent, fs._locator(action.source)[-1], action)
                observed, content = _file_evidence(fd, 'METADATA_RENAME' if self._metadata_rename(index) else 'FULL')
                if action.action != 'KEEP':
                    self._directories.target_absent(action)
                self._prepared[index] = None, before, observed, content
        self._check_execution()
        for index, action in enumerate(manifest.payload.actions):
            self._verify_prepared(index)
        from .target_execution_guard import _ScratchExecutionGuard
        self._phase = 'EXECUTION_GUARD'
        self._persistent_guard = self._resources.enter_context(closing(
            _ScratchExecutionGuard.create(self.database_path, self.root, manifest, self._maintenance_owner)))
        self._notify_progress('MANIFEST_CONFIRMED')
        self._notify_progress('FOLDER_TREE_PREPARING')
        self._directories.prepare()
        self._notify_progress('FOLDER_TREE_CONFIRMED')
        self._identities = tuple(self._resources.enter_context(
            ScratchIdentitySession._new_execution_context(self._filesystem, self, index))
            for index in range(len(manifest.payload.actions)))
        self._guards = tuple(_LiveIdentityChecks(context) for context in self._identities)

    @_filesystem_checkpoint
    def _verify_prepared(self, index):
        action = self._manifest.payload.actions[index]
        _, before, observed, _ = self._prepared[index]
        self._filesystem._check_root()
        with ExitStack() as paths:
            parent = self._filesystem._parent(fs._locator(action.source), paths)
            fd = os.open(fs._locator(action.source)[-1], fs._FILE, dir_fd=parent)
            paths.enter_context(fs._held_evidence_fd(fd))
            self._filesystem._check_fd(fd)
            current = fs._check_source(fd, parent, fs._locator(action.source)[-1], action, before)
            if _file_stat(current) != observed:
                raise fs.RenameRejected('STOP: preflight source changed')
            if action.action != 'KEEP':
                self._directories.target_absent(action)

    def _check_prepared(self, index, observation):
        self._verify_prepared(index)
        if _file_stat(observation._before) != self._prepared[index][2]:
            raise fs.RenameRejected('STOP: observation no longer matches the original preflight pin')

    @_filesystem_checkpoint
    def _require_predecessor(self):
        self._phase = 'DEPENDENCY_GATE'
        self._check_journal()
        if (len(self._history) != 5 * self._active_index or not self._history
                or self._history[-1].state != 'COMPLETE'
                or self._history[-1].action_index != self._active_index - 1):
            raise ScratchJournalReconciliationRequired('STOP: incomplete acknowledged manifest prefix', self._phase)
        self._phase = 'DB_PREDECESSOR'
        self._database._verify_stored(self._active_index - 1)
        self._phase = 'PREDECESSOR_IDENTITY'
        self._verify_physical(self._active_index - 1)
        self._check_journal()

    def _capture_indices(self):
        return tuple(index for index in (self._active_index - 1, self._active_index) if index in self._captures)

    def _verification_indices(self, index):
        # The last action also owns container patches. Prove every physical
        # target before those final locators may commit, once per final stage.
        return range(index + 1) if index == len(self._manifest.payload.actions) - 1 else range(max(0, index - 1), index + 1)

    def _check_journal(self):
        try:
            self._writer.checkpoint(self._manifest)
            if (len(self._history) != len(self._writer._entries)
                    or (self._history and self._history[-1] != self._writer._entries[-1])):
                raise ValueError('acknowledged journal prefix differs')
        except Exception as exc:
            raise ScratchJournalReconciliationRequired('STOP: live execution journal unconfirmed', self._phase) from exc

    @_filesystem_checkpoint
    def _append(self, state):
        result = super()._append(state)
        if self._progress is not None:
            # Observe only the acknowledged E1 prefix; never persist a second
            # per-action history or turn this notification into authority.
            completed = len(self._completed_actions)
            self._progress({'phase': state, 'action_index': self._active_index,
                'completed': completed, 'total': len(self._manifest.payload.actions),
                'remaining': len(self._manifest.payload.actions) - completed})
        return result

    def _notify_progress(self, phase):
        if self._progress is not None:
            completed = len(self._completed_actions)
            self._progress({'phase': phase, 'completed': completed,
                'total': len(self._manifest.payload.actions),
                'remaining': len(self._manifest.payload.actions) - completed})

    def _execute_actions(self):
        result = []
        for index in range(len(self._manifest.payload.actions)):
            self._active_index = index
            if index:
                self._require_predecessor()
            self._authorized_index = index
            result.append(self._run_move(index))
        self._require_journal()
        self._directories.check_bindings()
        self._notify_progress('ACTIONS_CONFIRMED')
        if self._cleanup_decision is not None:
            from .target_execution_cleanup import _run_cleanup
            self.cleanup_outcome = _run_cleanup(self, self._cleanup_decision)
            self._directories.removed = set(self.cleanup_outcome.removed)
            self._notify_progress('CLEANUP_CONFIRMED')
        else:
            for index in range(len(self._manifest.payload.actions)):
                self._verify_physical(index, full=True)
        if self._completion_rescan_requested:
            self._notify_progress('FINAL_VERIFICATION')
            from .scanner.execution_completion import run_completed_scratch_rescan
            self._phase = 'P2C_COMPLETION'
            self.completion_rescan_result = run_completed_scratch_rescan(self)
            if self._resume_scanning_requested:
                self._persistent_guard.enable_scanning(self)
        return tuple(result)

    @_filesystem_checkpoint
    def _identity_check(self, index, *, moved=None):
        action = self._manifest.payload.actions[index]
        if index in self._completed_stats:
            # A COMPLETE checkpoint is not replay authority. Its original live
            # owner still checks exact captured bytes/stat on a nofollow reopen.
            context = self._identities[index]
            captured, _, _, expected_stat = self._captures[index]
            if captured != context._captured_record or expected_stat != context._artifact_stat:
                raise IdentityEvidenceReconciliationRequired('STOP: original completed capture binding changed')
            raw = _frame(captured)
            with fs._held_evidence_fd(os.open(context.evidence_path.name, fs._FILE, dir_fd=self._storage_fd)) as fd:
                if (_file_stat(context._check_artifact(fd, self._storage_fd)) != expected_stat
                        or os.pread(fd, len(raw) + 1, 0) != raw
                        or _file_stat(context._check_artifact(fd, self._storage_fd)) != expected_stat):
                    raise IdentityEvidenceReconciliationRequired('STOP: completed identity evidence changed')
            return
        captured = self._captures[index][0]
        if captured.content != self._prepared[index][3]:
            raise ScratchFilesystemReconciliationRequired('STOP: capture differs from admitted evidence', self._phase)
        if self._metadata_rename(index):
            captured, fd, parent, before = self._captures[index]
            guard = self._guards[index]
            guard._check_identity_anchor(self._manifest, action, captured, fd, parent, before, self._phase)
            if moved is None:
                return
            comparison = self._identities[index].compare(self._manifest, action, action_index=index)
            source, target = comparison.source, comparison.target
            current = target if moved else source
            unique = (source is not None and target is not None and
                ((action.action == 'KEEP' and source == target and source.presence == 'PRESENT')
                 or (action.action != 'KEEP' and source.presence == ('ABSENT' if moved else 'PRESENT')
                     and target.presence == ('PRESENT' if moved else 'ABSENT'))))
            attributes = ('st_dev', 'st_ino', 'size', 'mtime_ns', 'file_type', 'nlink', 'mode', 'uid', 'gid')
            if (captured.version != 2 or captured.mode != 'METADATA_RENAME' or captured.content is not None
                    or comparison.outcome != 'METADATA_MATCH' or not unique or current is None
                    or current.content_match != 'NOT_CHECKED' or current.observed_content is not None
                    or current.physical_identity_match != 'MATCH' or current.filesystem_match != 'MATCH'
                    or current.observed_file is None
                    or any(getattr(current.observed_file, key) != getattr(captured.filesystem, key) for key in attributes)
                    or ((not moved or action.action == 'KEEP')
                        and current.observed_file.ctime_ns != captured.filesystem.ctime_ns)):
                raise ScratchFilesystemReconciliationRequired('STOP: metadata rename identity unconfirmed', self._phase)
            guard._check_identity_anchor(self._manifest, action, captured, fd, parent, before, self._phase)
            if moved:
                self._active_stats[index] = current.observed_file
            return current.observed_file
        if action.action != 'KEEP' or moved is None:
            observed = super()._identity_check(index, moved=moved)
            if moved is True:
                self._active_stats[index] = observed
            return observed
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
        if moved:
            self._active_stats[index] = comparison.target.observed_file

    def _run_move(self, index):
        # Only this active action retains source, parent and capture pins.
        # Never close them before all original fsync/E1/SQLite acknowledgements.
        with ExitStack() as resources:
            self._action_resources = resources
            context = self._identities[index]
            resources.callback(context.close)
            result = super()._run_move(index)
            with self._target_file(index) as fd:
                if _file_stat(os.fstat(fd)) != self._active_stats[index]:
                    raise ScratchFilesystemReconciliationRequired('STOP: file changed after MOVE identity verification', self._phase)
                self._completed_stats[index] = self._active_stats[index]
            captured, _, _, artifact_stat = self._captures[index]
            self._captures[index] = captured, None, None, artifact_stat
            return result

    @contextmanager
    def _target_file(self, index):
        """Nofollow reopen constrained to the original action and media mount."""
        action = self._manifest.payload.actions[index]
        parts = fs._locator(action.target)
        with ExitStack() as stack:
            self._filesystem._check_root()
            parent = self._filesystem._parent(parts, stack)
            fd = os.open(parts[-1], fs._FILE, dir_fd=parent)
            stack.enter_context(fs._held_evidence_fd(fd))
            self._filesystem._check_fd(fd)
            before = fs._check_source(fd, parent, parts[-1], action, self._prepared[index][1])
            if _file_stat(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != _file_stat(before):
                raise fs.RenameRejected('STOP: target changed during nofollow open')
            yield fd
            current = fs._check_source(fd, parent, parts[-1], action, self._prepared[index][1])
            self._filesystem._parent_binding(parts, parent)
            if (_file_stat(before) != _file_stat(current)
                    or _file_stat(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != _file_stat(current)):
                raise fs.RenameRejected('STOP: target changed during reopen')

    @_filesystem_checkpoint
    def _verify_physical(self, index, *, full=False):
        """Reopen exact metadata; read bytes only for explicitly selected FULL sources.

        METADATA_RENAME has no content digest and does not establish byte equality.
        ctime is a metadata fence, not a substitute for an absent content proof.
        """
        try:
            return self._verify_checkpoint(index, full=full)
        except ScratchCoordinationStopped:
            raise
        except Exception as exc:
            raise ScratchFilesystemReconciliationRequired('STOP: physical checkpoint unconfirmed', self._phase) from exc

    def _verify_checkpoint(self, index, *, full):
        if index not in self._completed_stats:
            # The active inode stays pinned through COMPLETE in both modes.
            if full and not self._metadata_rename(index):
                return super()._verify_physical(index)
            verification = self._observations[index].verify()
            if verification != self._verifications[index]:
                raise ScratchFilesystemReconciliationRequired('STOP: active physical state changed', self._phase)
            with self._target_file(index) as fd:
                if _file_stat(os.fstat(fd)) != self._active_stats[index]:
                    raise ScratchFilesystemReconciliationRequired('STOP: active content checkpoint changed', self._phase)
            self._identity_check(index)
            return
        self._identity_check(index)
        action = self._manifest.payload.actions[index]
        with self._target_file(index) as fd:
            expected = self._completed_stats[index]
            if _file_stat(os.fstat(fd)) != expected:
                raise ScratchFilesystemReconciliationRequired('STOP: completed file checkpoint changed', self._phase)
            if full and not self._metadata_rename(index) and _hash_file(fd) != (expected, self._prepared[index][3]):
                raise ScratchFilesystemReconciliationRequired('STOP: completed file SHA-256 changed', self._phase)
            if _file_stat(os.fstat(fd)) != expected:
                raise ScratchFilesystemReconciliationRequired('STOP: target changed during checkpoint', self._phase)
        if action.action != 'KEEP':
            parent_path = action.source.rsplit('/', 1)[0] if '/' in action.source else ''
            if parent_path not in self._directories.removed:
                with ExitStack() as paths:
                    parent = self._filesystem._parent(fs._locator(action.source), paths)
                    fs._absent(parent, fs._locator(action.source)[-1])
        self._identity_check(index)

    def _outcome(self, index, verification):
        action = self._manifest.payload.actions[index]
        return CompletedManifestAction(self._manifest.manifest_id, action.action_id, index, self.journal_path,
            self.database_path, action.action, action.object_kind, action.object_id, verification)
