"""Live scratch E7G completion bridge, not a persisted/replayable permission.

Only the original owner can consume its acknowledged E1 writer and completed
identity checkpoints. Loaded journals, outcomes and hashes grant no admission.
"""
import sqlite3
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app import target_execution_journal as journal
from app.target_execution_coordination import (
    ScratchCoordinationStopped, ScratchFilesystemReconciliationRequired,
)
from app.target_execution_manifest import JournalEntry


_PHASE = 'P2C_COMPLETION'


def _check_live_completion(owner):
    from app.target_execution_sequence import ScratchManifestExecution
    if type(owner) is not ScratchManifestExecution:
        raise ValueError('original live scratch execution owner required')
    owner._check_execution()  # Original PID/thread/mutex and exclusive E6B lease.
    if owner._phase != _PHASE or not owner._completion_rescan_requested:
        raise ValueError('completion rescan requires the original live verified completion boundary')
    if owner._cleanup_decision is not None and owner.cleanup_outcome is None:
        raise ValueError('requested cleanup has no verified outcome')
    actions = owner._manifest.payload.actions
    expected_indices = set(range(len(actions)))
    if (not actions or owner._active_index != len(actions) - 1
            or any(set(values) != expected_indices for values in
                (owner._captures, owner._observations, owner._verifications))):
        raise ValueError('complete original physical evidence required')
    if (type(owner._writer) is not journal.DurableJournal
            or owner._writer._path != owner.journal_path):
        raise ValueError('original live durable execution writer required')
    expected = tuple(JournalEntry(owner._manifest.manifest_id, action.action_id, index, state)
        for index, action in enumerate(actions)
        for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'))
    if owner._require_journal() != expected:
        raise ValueError('all original actions require acknowledged DB_RECONCILED and COMPLETE history')
    # Includes original R0-derived final rows/schema, independent committed DB
    # read, acknowledged commit count/fingerprint and the original post hash.
    if owner.cleanup_outcome is not None and owner.cleanup_outcome.removed:
        from app.target_execution_cleanup import read_completed_cleanup
        owner._cleanup_verifier._check()
        checked = read_completed_cleanup(owner.database_path, owner._manifest, owner.root)
        if checked != owner.cleanup_outcome:
            raise ValueError('live cleanup outcome differs from durable evidence')
        owner._cleanup_verifier._check()
        owner._database._verify_stored(len(actions) - 1, verify_post_hash=False)
        for index in range(len(actions)):
            owner._verify_physical(index)
    else:
        owner._database._verify_stored(len(actions) - 1)
        for index in range(len(actions)):
            owner._verify_physical(index, full=True)
    owner._require_journal()
    owner._check_execution()


def run_completed_scratch_rescan(owner):
    """Consume evidence in its original live owner; return diagnostics only.

    No API accepts a boolean completion assertion, reopened journal, result or
    copied token as authority. After close/restart this function refuses access.
    """
    from app.scanner.service import scan_library
    engine = None
    try:
        _check_live_completion(owner)
        # Independent mode=ro connection; do not reuse the executor's reserved
        # BEGIN IMMEDIATE connection or a caller Session with pending updates.
        engine = create_engine('sqlite://', creator=lambda: sqlite3.connect(
            owner._database._uri + '?mode=ro', uri=True, timeout=0))

        @event.listens_for(engine, 'begin')
        def snapshot(connection):
            connection.connection.driver_connection.execute('BEGIN')

        with Session(engine) as session:
            result = scan_library(session, owner.root, execution_manifest=owner._manifest,
                execution_cleanup=owner.cleanup_outcome if owner.cleanup_outcome and owner.cleanup_outcome.removed else None)
        _check_live_completion(owner)
        return result
    except Exception as exc:
        if isinstance(exc, ScratchCoordinationStopped) and exc.reconciliation_required:
            raise
        from app.target_execution_sequence import ScratchManifestExecution
        # A spent live owner may already have moved files and committed SQLite.
        # Losing admission evidence must never label that state as a safe reject.
        category = (ScratchFilesystemReconciliationRequired
            if type(owner) is ScratchManifestExecution and owner._spent
            else ScratchCoordinationStopped)
        raise category('STOP: P2C live completion evidence unconfirmed', _PHASE) from exc
    finally:
        if engine is not None:
            engine.dispose()
