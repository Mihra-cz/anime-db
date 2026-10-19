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
    _LiveIdentityChecks, ScratchCoordinationStopped, ScratchJournalReconciliationRequired,
)
from .target_execution_database import _ScratchPairDatabase
from .target_execution_sequence import _ExecutionSequence
from .target_execution_identity import (
    ScratchIdentitySession,
)
from .target_execution_manifest import ExecutionManifest, JournalEntry, manifest_to_json, manifest_windows_root


class _PairFilesystem(fs.ScratchRenameSession):
    def _validate(self, manifest, action):
        fs._validate_scratch_binding(manifest, action, self.root, self.mount_identity)
        owner = self._pair_owner
        if (os.getpid() != owner._pid or not owner._spent or owner._closed
                or not owner._mutex.locked() or threading.get_ident() != owner._execution_thread
                or manifest != owner._manifest
                or (action.depends_on and not owner._dependency_authorized(manifest, action))):
            raise fs.RenameRejected('STOP: no live pair-owner authorization')


class _ScratchVideoMovePair(_ExecutionSequence):
    """Factory-only, single-use owner of precisely A then dependent B."""

    def __init__(self):
        raise TypeError('use _ScratchVideoMovePair.create; no existing-context adoption')

    def _check_execution(self):
        # E6A retains its original _PairFilesystem and dependency capability.
        pass

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
        # Legacy v1 has no sealed final-verification root: STOP before DB, evidence or journal.
        manifest_windows_root(manifest)
        self._manifest_bytes = manifest_to_json(manifest)

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

    def execute(self, manifest: ExecutionManifest):
        return self._execute_once(manifest)

    def _prepare_execution(self, manifest):
        self._validate_pair(manifest)
        self._manifest = manifest
        self._phase = 'DB_VALIDATION'
        self._database = self._resources.enter_context(closing(
            _ScratchPairDatabase(self._filesystem, self.database_path, manifest)))

    def _execute_actions(self):
        first = self._run_move(0)
        self._active_index = 1
        self._require_predecessor()
        self._authorized_b = self._manifest
        second = self._run_move(1)
        return first, second
