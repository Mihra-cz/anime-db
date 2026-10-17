"""Manually approved, one-shot inverse MOVE on one original synthetic scratch case.

No execution-session adoption, E1 append, DB writes, automatic recovery or retry.
The separate O_EXCL record is never reopened; even a partial record blocks reuse.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass, replace
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import quote

from . import maintenance
from . import target_execution_diagnostics as diagnosis
from . import target_execution_filesystem as fs
from . import target_execution_identity as identity
from . import target_execution_journal as journal
from . import target_execution_reconciliation as inspection
from .target_execution_manifest import _digest, manifest_to_json


IDENTITY_LIMITATION = (
    'Historical inode continuity is UNKNOWN after restart. Matching SHA-256 and '
    'numeric metadata do not prove original physical identity; this approval '
    'authorizes only returning the currently verified target to the original source.'
)


class ScratchRollbackStopped(ValueError):
    """STOP; preserve actual files and evidence. No retry or resume authority."""

    def __init__(self, message, phase='VALIDATION', *, reconciliation_required=False):
        super().__init__(message)
        self.phase = phase
        self.reconciliation_required = reconciliation_required


@dataclass(frozen=True)
class RollbackProposal:
    manifest_sha256: str
    diagnosis: diagnosis.ScratchVideoDiagnosis
    rollback_source: str
    rollback_target: str
    proposal_sha256: str
    identity_continuity: str = 'UNKNOWN'
    identity_limitation: str = IDENTITY_LIMITATION

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class RollbackApproval:
    """Separate explicit caller input; diagnostics never construct an approval."""

    proposal: RollbackProposal
    decision: str
    acknowledgement: str
    approved_by: str


@dataclass(frozen=True)
class ScratchRollbackOutcome:
    manifest_id: str
    action_id: str
    action_index: int
    evidence_path: Path
    outcome: str = 'ROLLED_BACK'
    identity_continuity: str = 'UNKNOWN'


def propose_scratch_video_rollback(manifest, action, *, action_index, scratch_root,
                                   journal_path, evidence_path) -> RollbackProposal:
    """Read-only proposal, never write authority. Original journal must be incomplete."""
    try:
        result = diagnosis.inspect_scratch_video_move(manifest, action,
            action_index=action_index, scratch_root=scratch_root,
            journal_path=journal_path, evidence_path=evidence_path)
        physical, database = result.filesystem, result.database
        target = physical.target
        allowed = {'IDENTITY_CONTINUITY_UNPROVEN', 'PENDING_WITH_TARGET_ONLY',
            'TARGET_ONLY_WITH_OLD_VALUES', 'MANUAL_REVIEW_REQUIRED', 'FULL_DB_POST_STATE_UNPROVEN'}
        if (physical.observed_state != 'TARGET_ONLY' or physical.journal.status != 'VALID'
                or physical.journal.last_action_phase not in {'PENDING', 'FS_APPLIED', 'FS_VERIFIED'}
                or physical.evidence.status != 'VALID' or target is None
                or target.filesystem_match != 'MATCH' or target.content_match != 'MATCH'
                or database.status != 'DB_INSPECTED' or database.locator_state != 'OLD_VALUES'
                or database.original_fingerprint_match is not True
                or set(result.stop_reasons) - allowed):
            raise ScratchRollbackStopped('STOP: not the supported interrupted TARGET_ONLY / OLD_VALUES case')
        proposal = RollbackProposal(hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest(),
            result, action.target, action.source, '')
        return replace(proposal, proposal_sha256=_digest(proposal.as_dict()))
    except ScratchRollbackStopped:
        raise
    except Exception as exc:
        raise ScratchRollbackStopped('STOP: rollback proposal could not be verified') from exc


def _owner(owner, root):
    if (type(owner) is not maintenance._Lease or not owner.exclusive
            or owner._thread != threading.get_ident()
            or type(owner.boundary) is not maintenance.ScratchMaintenance):
        raise ScratchRollbackStopped('STOP: live exclusive E6B maintenance owner required')
    gate = owner.boundary
    gate.check_process()
    gate._check_binding(owner)
    if gate.library_root != root or gate.database_path != root.parent / (root.name + '.sqlite'):
        raise ScratchRollbackStopped('STOP: foreign maintenance database/root binding')


def rollback_scratch_video_move(manifest, action, *, action_index, scratch_root,
                                journal_path, evidence_path, approval=None,
                                maintenance_owner=None) -> ScratchRollbackOutcome:
    """One separately approved return, under the caller's exclusive maintenance.

    A new live pin protects only this attempt, never historical inode continuity.
    A durable ATTEMPT without VERIFIED requires manual inspection; no API here
    can reopen it, retry it or undo an uncertain syscall. SQLite is read-only.
    """
    phase, attempted = 'VALIDATION', False
    try:
        if (type(approval) is not RollbackApproval or type(approval.proposal) is not RollbackProposal
                or approval.decision != 'RETURN_TARGET_TO_SOURCE'
                or approval.acknowledgement != IDENTITY_LIMITATION
                or type(approval.approved_by) is not str or not approval.approved_by.strip()):
            raise ScratchRollbackStopped('STOP: explicit bound approval and identity acknowledgement required')
        root, journal_path, evidence_path = Path(scratch_root), Path(journal_path), Path(evidence_path)
        diagnosis._scope(manifest, action, action_index, root, journal_path, evidence_path)
        _owner(maintenance_owner, root)
        proposed = propose_scratch_video_rollback(manifest, action, action_index=action_index,
            scratch_root=root, journal_path=journal_path, evidence_path=evidence_path)
        if approval.proposal != proposed:
            raise ScratchRollbackStopped('STOP: approval does not bind the current manifest/action/FS/DB proposal')
        report, database = proposed.diagnosis.filesystem, proposed.diagnosis.database
        record_path = root.parent / (root.name + '.rollback.jsonl')
        with ExitStack() as stack:
            mounts = fs._read_mounts()
            parent = stack.enter_context(fs._held_evidence_fd(fs._open_absolute(root.parent)))
            root_fd = stack.enter_context(fs._held_evidence_fd(os.open(root.name, fs._DIRECTORY, dir_fd=parent)))
            fs._secure_directory(root_fd)
            mount = fs._check_mounts(root, root_fd, mounts)
            fs._check_mounts(root.parent, parent, mounts)
            root_identity = identity._directory(root_fd)
            if (root_identity.st_dev != os.fstat(parent).st_dev
                    or fs._mount_id(parent) != mount.mount_id):
                raise ScratchRollbackStopped('STOP: scratch root/record mount crossing')
            fs._validate_scratch_action(manifest, action, root,
                f'{mount.fstype}:{mount.source}:{root_identity.st_dev}:{root_identity.st_ino}')
            anchors = []
            history = inspection._journal(journal_path, parent, mount.mount_id, stack, anchors, manifest, action_index)
            captured = inspection._evidence(evidence_path, parent, mount, mounts, root, root_fd,
                stack, anchors, manifest, action, action_index)
            if history != report.journal or captured != report.evidence:
                raise ScratchRollbackStopped('STOP: original journal/identity evidence changed')
            db_path = Path(database.path)
            db_artifact = diagnosis._artifact(db_path, '', parent, mount.mount_id, stack, anchors)
            if db_artifact != database.artifacts[0]:
                raise ScratchRollbackStopped('STOP: approved SQLite changed')
            anchor_content = {fd: identity._hash_file(fd) for _, fd, _ in anchors}
            proof = captured.record
            old_parts, new_parts = fs._locator(action.source), fs._locator(action.target)
            old_parent = identity._current_parent(root_fd, old_parts, stack, proof.binding.source_parent, mount.mount_id)
            new_parent = identity._current_parent(root_fd, new_parts, stack, proof.binding.target_parent, mount.mount_id)
            observed, file_fd = identity._locator_comparison(action.target, new_parent, proof, stack, mount.mount_id)
            if observed != report.target:
                raise ScratchRollbackStopped('STOP: approved target changed before pinning')
            before = os.fstat(file_fd)
            record_fd, record_stat, record_bytes, post_file = None, None, b'', None

            def check_record():
                if record_fd is not None and (
                        identity._file_stat(journal._check_file(record_fd, parent, record_path.name)) != record_stat
                        or os.pread(record_fd, len(record_bytes) + 1, 0) != record_bytes):
                    raise ScratchRollbackStopped('STOP: rollback evidence changed')

            def stable(*, moved=False):
                nonlocal post_file
                _owner(maintenance_owner, root)
                journal._check_parent(record_path, parent)
                check_record()
                with fs._held_evidence_fd(fs._open_absolute(root)) as fresh:
                    if (identity._directory(fresh) != root_identity or fs._mount_id(fresh) != mount.mount_id
                            or fs._read_mounts() != mounts):
                        raise ScratchRollbackStopped('STOP: scratch root/mount changed')
                    fs._check_mounts(root, fresh, mounts)
                with ExitStack() as parents:
                    identity._current_parent(root_fd, old_parts, parents, proof.binding.source_parent, mount.mount_id)
                    identity._current_parent(root_fd, new_parts, parents, proof.binding.target_parent, mount.mount_id)
                for path, fd, metadata in anchors:
                    if (identity._file_stat(journal._check_file(fd, parent, path.name)) != metadata
                            or fs._mount_id(fd) != mount.mount_id
                            or identity._hash_file(fd) != anchor_content[fd]):
                        raise ScratchRollbackStopped('STOP: original DB/journal/evidence changed')
                for suffix in ('-journal', '-wal', '-shm'):
                    fs._absent(parent, db_path.name + suffix)
                absent_parent, absent_name = (new_parent, new_parts[-1]) if moved else (old_parent, old_parts[-1])
                present_parent, present_name = (old_parent, old_parts[-1]) if moved else (new_parent, new_parts[-1])
                fs._absent(absent_parent, absent_name)
                current = fs._check_source(file_fd, present_parent, present_name, action, before)
                actual, content = identity._hash_file(file_fd)
                attributes = ('st_dev', 'st_ino', 'size', 'mtime_ns', 'file_type', 'nlink', 'mode', 'uid', 'gid')
                if (content != proof.content or any(getattr(actual, name) != getattr(observed.observed_file, name)
                        for name in attributes) or (not moved and current.st_ctime_ns != before.st_ctime_ns)):
                    raise ScratchRollbackStopped('STOP: live target identity/content changed')
                if moved:
                    if post_file is not None and actual != post_file:
                        raise ScratchRollbackStopped('STOP: file changed after rollback observation')
                    with ExitStack() as independent:
                        comparison, _ = identity._locator_comparison(action.source, old_parent, proof,
                            independent, mount.mount_id, pin_fd=file_fd)
                        if (comparison.content_match != 'MATCH' or comparison.filesystem_match != 'MATCH'
                                or comparison.physical_identity_match != 'MATCH'):
                            raise ScratchRollbackStopped('STOP: independent rollback verification failed')
                    post_file = actual

            stable()
            fs._absent(parent, record_path.name)
            # Retain E5B's read transaction across the physical return. No
            # immutable/nolock mode, writer DB scope or SQLite mutation is used.
            with closing(sqlite3.connect('file:' + quote(str(db_path), safe='/') + '?mode=ro',
                    uri=True, timeout=0, isolation_level=None)) as connection:
                connection.execute('PRAGMA query_only=ON')
                connection.execute('PRAGMA trusted_schema=OFF')
                connection.execute('BEGIN')
                current_db = diagnosis._read_values(connection, action, database)
                if current_db != database:
                    raise ScratchRollbackStopped('STOP: SQLite no longer has the approved original locators')
                stable()
                phase = 'APPROVAL_EVIDENCE'
                record_fd = os.open(record_path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL
                    | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
                stack.enter_context(fs._held_evidence_fd(record_fd))
                journal._lock(record_fd, True)
                record_stat = identity._file_stat(journal._check_file(record_fd, parent, record_path.name))
                sequence, previous = 0, None

                def append(state, **details):
                    nonlocal sequence, previous, record_bytes, record_stat
                    _owner(maintenance_owner, root)
                    journal._check_parent(record_path, parent)
                    check_record()
                    raw, checksum = journal._frame(dict(schema='animedb-scratch-video-rollback', version=1,
                        sequence=sequence, previous_sha256=previous, state=state,
                        proposal_sha256=proposed.proposal_sha256, **details))
                    journal._write_all(record_fd, raw)
                    os.fsync(record_fd)
                    record_bytes += raw
                    record_stat = identity._file_stat(journal._check_file(record_fd, parent, record_path.name))
                    check_record()
                    previous, sequence = checksum, sequence + 1

                append('APPROVED', approval=asdict(approval))
                os.fsync(parent)
                stable()
                phase = 'ATTEMPT_EVIDENCE'
                append('ATTEMPT', outcome='UNKNOWN_UNTIL_VERIFIED', automatic_retry=False)
                stable()
                phase, attempted = 'RENAME', True
                fs._rename_noreplace(new_parent, new_parts[-1], old_parent, old_parts[-1])
                phase = 'VERIFICATION'
                stable(moved=True)
                phase = 'DURABILITY'
                # Same E2A ordering: file, new name, removal of previous name.
                os.fsync(file_fd)
                os.fsync(old_parent)
                if identity._directory(new_parent) != identity._directory(old_parent):
                    os.fsync(new_parent)
                phase = 'VERIFICATION'
                stable(moved=True)
            # Closing the reader must leave the DB and its sidecars unchanged.
            stable(moved=True)
            phase = 'RESULT_EVIDENCE'
            append('VERIFIED', database='UNCHANGED', identity_continuity='UNKNOWN',
                returned_to=action.source, removed_from=action.target,
                filesystem=asdict(post_file), content=asdict(proof.content), durability_confirmed=True)
            stable(moved=True)
        return ScratchRollbackOutcome(manifest.manifest_id, action.action_id, action_index, record_path)
    except (KeyboardInterrupt, SystemExit) as exc:
        exc.add_note(f'Scratch rollback STOP in {phase}; preserve actual state/evidence; no retry. '
            f'Rename attempted: {attempted}.')
        raise
    except Exception as exc:
        if isinstance(exc, ScratchRollbackStopped) and not attempted:
            exc.phase = phase
            raise
        raise ScratchRollbackStopped('STOP: scratch rollback failed; preserve actual state and evidence; no retry',
            phase, reconciliation_required=attempted) from exc
