"""Live E7G completion -> independent P2C-R1 rescan; never restart admission."""
import gc
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import target_execution_database as database
from app import target_execution_journal as journal
from app import target_execution_sequence as sequence
from app.scanner import execution_completion as completion
from app.target_execution_coordination import ScratchCoordinationStopped
from app.target_execution_manifest import manifest_to_json, manifest_from_json
from test_v6_execution_file_actions import prepare_mixed
from test_v6_p2c_rescan import approve

from app.maintenance import ScratchMaintenance, MaintenanceStopped
from app.target_execution_sequence import ScratchManifestExecution
from test_v6_execution_sequence import prepare_sequence
from test_v6_execution_projection import expected_rows
from v6_database_case import database_rows


def test_complete_manifest_two_moves_keep_then_independent_rescan(tmp_path, monkeypatch):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_sequence(owner, count=2, windows_root=r'C:\Anime')
        assert sum(a.action == 'MOVE' for a in manifest.payload.actions) == 2
        assert any(a.action == 'KEEP' for a in manifest.payload.actions)
        before = database_rows(path)
        gc.collect()
        from app.scanner import service
        real_scan = service.scan_library
        def excluded_reader(*args, **kwargs):
            code = """from pathlib import Path
import sys
from app.maintenance import ScratchMaintenance, MaintenanceBusy
try:
 with ScratchMaintenance(Path(sys.argv[1]),Path(sys.argv[2])).writer():
  raise AssertionError('writer was not excluded')
except MaintenanceBusy:
 print('excluded')
"""
            for stage in ('before', 'after'):
                if stage == 'after':
                    result = real_scan(*args, **kwargs)
                child = subprocess.run([sys.executable, '-c', code, str(path), str(owner.root)],
                    capture_output=True, text=True, timeout=20)
                assert child.returncode == 0 and child.stdout.strip() == 'excluded', child.stderr
            return result
        monkeypatch.setattr(service, 'scan_library', excluded_reader)
        with ScratchMaintenance(path, owner.root).maintenance() as lease:
            outcomes = owner.execute(manifest, maintenance_owner=lease, completion_rescan=True)
        assert len(outcomes) == len(manifest.payload.actions)
        result = owner.completion_rescan_result
        assert (result.found, result.unchanged, result.created, result.updated, result.removed, result.errors) == (3, 3, 0, 0, 0, 0)
        assert owner._closed
        assert database_rows(path) == expected_rows(before, manifest.payload.locator_patches)


@pytest.fixture
def case(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_sequence(owner, count=2, windows_root=r'C:\Anime')
        gc.collect()
        yield owner, manifest, path


def run(case):
    owner, manifest, path = case
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        return owner.execute(manifest, maintenance_owner=lease, completion_rescan=True)


def test_mixed_duplicate_and_subtitle_evidence_survives_live_rescan(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_mixed(owner)
        manifest = approve(manifest)
        before, sealed = database_rows(path), manifest_to_json(manifest)
        assert manifest.payload.duplicate_evidence
        gc.collect()
        with ScratchMaintenance(path, owner.root).maintenance() as lease:
            owner.execute(manifest, maintenance_owner=lease, completion_rescan=True)
        assert owner.completion_rescan_result.unchanged == 3
        assert database_rows(path) == expected_rows(before, manifest.payload.locator_patches)
        assert manifest_to_json(manifest) == sealed


@pytest.mark.parametrize('fault', ['missing_complete', 'missing_reconciled', 'truncated_journal',
    'manifest_envelope', 'manifest_carried', 'uncommitted_db', 'identity', 'maintenance', 'reader_boundary'])
def test_live_completion_refuses_invalid_evidence(case, monkeypatch, fault):
    owner, manifest, path = case
    bridge = completion.run_completed_scratch_rescan
    original = database_rows(path)
    if fault in {'missing_complete', 'missing_reconciled'}:
        appended = sequence._ExecutionSequence._append
        def append(current, state):
            suppressed = {'COMPLETE'} if fault == 'missing_complete' else {'DB_RECONCILED', 'COMPLETE'}
            if current._active_index == len(manifest.payload.actions) - 1 and state in suppressed:
                return  # Simulate an incomplete acknowledged final prefix, not a fake loaded COMPLETE.
            return appended(current, state)
        monkeypatch.setattr(sequence._ExecutionSequence, '_append', append)

    def damaged(current):
        before = database_rows(path)
        if fault == 'truncated_journal':
            with current.journal_path.open('r+b') as stream:
                stream.truncate(stream.seek(0, 2) - 10)
        elif fault == 'manifest_envelope':
            current._manifest = replace(current._manifest, created_at='2026-10-23T12:00:01Z')
        elif fault == 'manifest_carried':
            from app.target_execution_manifest import ManifestDuplicateEvidence, _digest
            from dataclasses import asdict
            payload = replace(current._manifest.payload, duplicate_evidence=(
                ManifestDuplicateEvidence(301, 100, 'valid', (), 'UNKNOWN', ()),))
            current._manifest = replace(current._manifest, payload=payload, manifest_id=_digest(asdict(payload)))
            manifest_to_json(current._manifest)  # Well-formed, but not the original approved manifest.
        elif fault == 'uncommitted_db':
            # Executor holds its original reservation; independent reads still
            # observe committed Rn. Admission must also reject pending DB writes.
            current._database._connection.execute("UPDATE videos SET filename='pending' WHERE id=100")
            assert database_rows(path) == before
        elif fault == 'identity':
            evidence = current.evidence_paths[0]
            evidence.write_bytes(b'forged physical identity evidence')
        elif fault == 'maintenance':
            current._maintenance_owner.close()
        elif fault == 'reader_boundary':
            from app.scanner import service
            real_scan = service.scan_library
            def lost_during_read(*args, **kwargs):
                result = real_scan(*args, **kwargs)
                current._maintenance_owner.close()
                return result
            monkeypatch.setattr(service, 'scan_library', lost_during_read)
        return bridge(current)

    monkeypatch.setattr(completion, 'run_completed_scratch_rescan', damaged)
    if fault in {'maintenance', 'reader_boundary'}:
        # Check refusal at execute(), before E6B's own exit check can provide
        # an overlapping STOP. The outer check detects the closed lease too.
        with pytest.raises(MaintenanceStopped):
            with ScratchMaintenance(path, owner.root).maintenance() as lease:
                with pytest.raises(ScratchCoordinationStopped) as caught:
                    owner.execute(manifest, maintenance_owner=lease, completion_rescan=True)
    else:
        with pytest.raises(ScratchCoordinationStopped) as caught:
            run(case)
    assert caught.value.reconciliation_required is True
    assert caught.value.phase == 'P2C_COMPLETION'
    assert owner._stop_class is type(caught.value)
    assert owner.completion_rescan_result is None and owner._closed
    # All physical actions and original DB commits already finished; the failed
    # read-only admission must not rewrite their committed locator-only result.
    assert all((owner.root / a.target).is_file() for a in manifest.payload.actions)
    assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)


@pytest.mark.parametrize('moment', ['pending', 'renamed', 'fs_applied', 'commit_uncertain', 'append_fsync', 'uncommitted_final'])
def test_interrupted_lifecycle_never_publishes_completion(case, monkeypatch, moment):
    owner, manifest, path = case
    original = database_rows(path)
    append = sequence._ExecutionSequence._append
    commit = database._ScratchSequenceDatabase._commit
    def appended(current, state):
        if current._active_index == 0:
            if moment == 'pending' and state == 'PENDING':
                append(current, state)
                raise OSError('stop before rename')
            if moment == 'renamed' and state == 'FS_APPLIED':
                raise OSError('rename finished, no DB patch')
            if moment == 'fs_applied' and state == 'FS_APPLIED':
                append(current, state)
                raise OSError('FS_APPLIED only')
        return append(current, state)
    def committed(db, index):
        if moment == 'uncommitted_final' and index == len(manifest.payload.actions) - 1:
            assert db._connection.in_transaction
            return  # Actual final locator UPDATE remains uncommitted.
        commit(db, index)
        if moment == 'commit_uncertain' and index == 0:
            raise OSError('commit acknowledgement lost')
    monkeypatch.setattr(sequence._ExecutionSequence, '_append', appended)
    monkeypatch.setattr(database._ScratchSequenceDatabase, '_commit', committed)
    if moment == 'append_fsync':
        real = journal.DurableJournal.append
        def unacknowledged(writer, entry):
            if entry.state == 'COMPLETE':
                sync = os.fsync
                def fail(fd):
                    if fd == writer._fd:
                        raise OSError('journal fsync acknowledgement lost')
                    return sync(fd)
                with monkeypatch.context() as fault:
                    fault.setattr(os, 'fsync', fail)
                    return real(writer, entry)
            return real(writer, entry)
        monkeypatch.setattr(journal.DurableJournal, 'append', unacknowledged)
    with pytest.raises(ScratchCoordinationStopped):
        run(case)
    assert owner.completion_rescan_result is None and owner._closed
    with pytest.raises(ScratchCoordinationStopped):
        completion.run_completed_scratch_rescan(owner)
    if moment in {'pending', 'renamed', 'fs_applied'}:
        assert database_rows(path) == original
    if moment == 'uncommitted_final':
        from app.models import Video
        action = manifest.payload.actions[-1]
        assert action.object_kind == 'video' and action.action == 'MOVE'
        names = list(Video.__table__.columns.keys())
        persisted = next(row for row in database_rows(path)['videos'] if row[names.index('id')] == action.object_id)
        assert persisted[names.index('relative_path')] == action.source
        assert (owner.root / action.target).is_file()


def test_forgeries_closed_owner_and_reloaded_complete_are_not_capabilities(case):
    owner, manifest, path = case
    for forged in (True, SimpleNamespace(complete=True, manifest_id=manifest.manifest_id)):
        with pytest.raises(ScratchCoordinationStopped):
            completion.run_completed_scratch_rescan(forged)
    run(case)
    snapshot = journal.load_journal(owner.journal_path, manifest)
    assert snapshot.reconciliation_required
    assert snapshot.entries[-1].state == 'COMPLETE'
    for forged in (snapshot, owner.completion_rescan_result, owner):
        with pytest.raises(ScratchCoordinationStopped):
            completion.run_completed_scratch_rescan(forged)


def test_cleanup_cannot_be_combined_with_completion_rescan(case):
    owner, manifest, path = case
    before = database_rows(path)
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(ScratchCoordinationStopped):
            owner.execute(manifest, maintenance_owner=lease, completion_rescan=True,
                cleanup_decision=lambda proposal: None)
    assert not owner.journal_path.exists() and database_rows(path) == before


@pytest.mark.parametrize('moment', ['pending', 'complete'])
def test_sigkill_and_new_process_cannot_adopt_history(tmp_path, moment):
    # The child uses the actual executor and dies with an acknowledged PENDING,
    # before rename. The restarted process only inspects bytes and must STOP.
    code = '''import json,os,signal,sys
from pathlib import Path
from app.maintenance import ScratchMaintenance, MaintenanceStopped
from app.target_execution_sequence import ScratchManifestExecution
from app.target_execution_manifest import manifest_to_json
from test_v6_execution_sequence import prepare_sequence
from app import target_execution_sequence as sequence
parent=Path(sys.argv[1])
with ScratchManifestExecution.create(parent) as owner:
 manifest,path=prepare_sequence(owner,count=2,windows_root=r'C:\\Anime')
 (parent/'restart.json').write_text(json.dumps(dict(root=str(owner.root),db=str(path),manifest=manifest_to_json(manifest))))
 original=sequence._ExecutionSequence._append
 def stop(current,state):
  original(current,state)
  if (sys.argv[2]=='pending' and state=='PENDING') or (sys.argv[2]=='complete' and state=='COMPLETE' and current._active_index==len(manifest.payload.actions)-1): os.kill(os.getpid(),signal.SIGKILL)
 sequence._ExecutionSequence._append=stop
 with ScratchMaintenance(path,owner.root).maintenance() as lease:
  owner.execute(manifest,maintenance_owner=lease,completion_rescan=True)
'''
    env = {**os.environ, 'PYTHONPATH': str(Path.cwd()) + os.pathsep + str(Path.cwd() / 'tests')}
    child = subprocess.run([sys.executable, '-c', code, str(tmp_path), moment], env=env,
        capture_output=True, text=True, timeout=180)
    assert child.returncode == -signal.SIGKILL, child.stderr
    data = json.loads((tmp_path / 'restart.json').read_text())
    manifest = manifest_from_json(data['manifest'])
    root = Path(data['root'])
    snapshot = journal.load_journal(root.with_name(root.name + '.journal.jsonl'), manifest)
    assert snapshot.reconciliation_required
    if moment == 'pending':
        assert [e.state for e in snapshot.entries] == ['PENDING']
    else:
        assert snapshot.entries[-1].state == 'COMPLETE'
        assert len(snapshot.entries) == 5 * len(manifest.payload.actions)
    before = database_rows(Path(data['db']))
    with pytest.raises(ScratchCoordinationStopped):
        completion.run_completed_scratch_rescan(snapshot)
    assert database_rows(Path(data['db'])) == before


@pytest.mark.parametrize('assertion', [1, {'complete': True}, SimpleNamespace(complete=True)])
def test_caller_completion_assertion_is_only_a_request_and_cannot_grant_authority(case, assertion):
    owner, manifest, path = case
    before = database_rows(path)
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(ScratchCoordinationStopped):
            owner.execute(manifest, maintenance_owner=lease, completion_rescan=assertion)
    assert owner.completion_rescan_result is None
    assert not owner.journal_path.exists() and database_rows(path) == before


def test_close_failure_does_not_publish_completion_receipt(case, monkeypatch):
    owner, manifest, path = case
    original = database_rows(path)
    real = completion.run_completed_scratch_rescan
    def fail_close(current):
        result = real(current)
        def interrupted_close():
            raise OSError('resource close acknowledgement lost')
        current._resources.callback(interrupted_close)
        return result
    monkeypatch.setattr(completion, 'run_completed_scratch_rescan', fail_close)
    with pytest.raises(ScratchCoordinationStopped):
        run(case)
    assert owner.completion_rescan_result is None
    assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)


def completion_state(owner, path):
    """Independent committed rows plus immutable bytes/stat of reached artifacts."""
    import hashlib
    files = set(owner.root.rglob('*'))
    for item in owner.root.parent.glob(owner.root.name + '*'):
        files.add(item)
        if item.is_dir():
            files.update(item.rglob('*'))
    observed = {}
    for item in files:
        if item.is_file():
            info = item.stat()
            observed[str(item)] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                hashlib.sha256(item.read_bytes()).hexdigest())
    return database_rows(path), observed


@pytest.mark.parametrize('fault', ['journal', 'identity', 'r1', 'strong_e7'])
def test_post_completion_stop_preserves_reconciliation_and_reached_state(case, monkeypatch, fault):
    from app.scanner import service
    from app.scanner.service import LibrarySafetyError
    from app.target_execution_coordination import (
        ScratchDatabaseReconciliationRequired, ScratchFilesystemReconciliationRequired,
        ScratchJournalReconciliationRequired,
    )
    owner, manifest, path = case
    original = database_rows(path)
    real_bridge = completion.run_completed_scratch_rescan
    snapshot = {}
    root_cause = OSError('original E7 cause')
    strong = ScratchDatabaseReconciliationRequired('original E7 reconciliation', 'DB_FINAL_VERIFICATION')
    r1_error = LibrarySafetyError('R1 refused the completed projection')
    def rejected(*args, **kwargs):
        if fault == 'strong_e7':
            raise strong from root_cause
        raise r1_error
    def damaged(current):
        assert current._require_journal()[-1].state == 'COMPLETE'
        if fault == 'journal':
            raw = current.journal_path.read_bytes()
            current.journal_path.write_bytes(raw[:-10])
        elif fault == 'identity':
            current.evidence_paths[0].write_bytes(b'corrupted identity evidence')
        else:
            monkeypatch.setattr(service, 'scan_library', rejected)
        snapshot['reached'] = completion_state(current, path)
        return real_bridge(current)
    monkeypatch.setattr(completion, 'run_completed_scratch_rescan', damaged)
    with pytest.raises(ScratchCoordinationStopped) as caught:
        run(case)
    error = caught.value
    assert error.reconciliation_required is True
    assert owner._stop_class is type(error) and owner._phase == error.phase
    if fault == 'strong_e7':
        assert error is strong and error.phase == 'DB_FINAL_VERIFICATION'
        assert error.__cause__ is root_cause
    else:
        assert error.phase == 'P2C_COMPLETION'
        assert error.__cause__ is not None
        if fault == 'journal':
            assert type(error) is ScratchJournalReconciliationRequired
        else:
            assert type(error) is ScratchFilesystemReconciliationRequired
        if fault == 'r1':
            assert error.__cause__ is r1_error
    assert completion_state(owner, path) == snapshot['reached']
    assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
    assert owner.completion_rescan_result is None
    with pytest.raises(ScratchCoordinationStopped) as repeated:
        owner.execute(manifest, completion_rescan=True)
    assert type(repeated.value) is type(error)
    assert repeated.value.reconciliation_required is True and repeated.value.phase == error.phase
    assert completion_state(owner, path) == snapshot['reached']


@pytest.mark.parametrize('kind', ['weak_stop', 'rename_rejected'])
def test_sequence_promotes_direct_post_completion_rejection(case, monkeypatch, kind):
    from app.target_execution_filesystem import RenameRejected
    from app.target_execution_coordination import ScratchFilesystemReconciliationRequired
    owner, manifest, path = case
    rejection = ScratchCoordinationStopped('late safe-shaped STOP', 'VALIDATION') if kind == 'weak_stop' else RenameRejected('late rename-shaped rejection')
    reached = {}
    def rejected(current):
        assert current._require_journal()[-1].state == 'COMPLETE'
        reached['state'] = completion_state(current, path)
        raise rejection
    monkeypatch.setattr(completion, 'run_completed_scratch_rescan', rejected)
    with pytest.raises(ScratchCoordinationStopped) as caught:
        run(case)
    error = caught.value
    assert type(error) is ScratchFilesystemReconciliationRequired
    assert error.reconciliation_required is True and error.phase == 'P2C_COMPLETION'
    assert error.__cause__ is rejection
    assert owner._stop_class is type(error) and owner._phase == error.phase
    assert completion_state(owner, path) == reached['state']
    with pytest.raises(ScratchCoordinationStopped) as repeated:
        owner.execute(manifest, completion_rescan=True)
    assert type(repeated.value) is type(error) and repeated.value.reconciliation_required
    assert repeated.value.phase == 'P2C_COMPLETION'
    assert completion_state(owner, path) == reached['state']


def test_safe_pre_execution_rejection_stays_non_reconciliation(case):
    owner, manifest, path = case
    gate = ScratchMaintenance(path, owner.root)
    before = completion_state(owner, path)
    with gate.maintenance() as lease:
        with pytest.raises(ScratchCoordinationStopped) as caught:
            owner.execute(manifest, maintenance_owner=lease, completion_rescan={'complete': True})
        assert type(caught.value) is ScratchCoordinationStopped
        assert caught.value.reconciliation_required is False and caught.value.phase == 'VALIDATION'
        assert caught.value.__cause__ is not None
        assert owner._stop_class is ScratchCoordinationStopped
        with pytest.raises(ScratchCoordinationStopped) as repeated:
            owner.execute(manifest, maintenance_owner=lease, completion_rescan=True)
        assert not repeated.value.reconciliation_required and repeated.value.phase == 'VALIDATION'
    assert completion_state(owner, path) == before
    assert not owner.journal_path.exists()


@pytest.mark.parametrize('boundary', ['partial_actions', 'wrong_phase', 'not_requested', 'cleanup'])
def test_K06_bridge_only_at_requested_completion_boundary(case, monkeypatch, boundary):
    owner, manifest, path = case
    original = database_rows(path)
    bridge = completion.run_completed_scratch_rescan
    reached = {}
    def attempted(current):
        reached['phase'] = current._phase
        reached['requested'] = current._completion_rescan_requested
        reached['state'] = completion_state(current, path)
        return bridge(current)
    if boundary == 'cleanup':
        def decision(proposal):
            assert owner._phase == 'CLEANUP' and owner._cleanup_decision is decision
            attempted(owner)
            return None
        with ScratchMaintenance(path, owner.root).maintenance() as lease:
            with pytest.raises(ScratchCoordinationStopped) as caught:
                owner.execute(manifest, maintenance_owner=lease, cleanup_decision=decision)
    else:
        move = sequence._ExecutionSequence._run_move
        def moved(current, index):
            result = move(current, index)
            target = 0 if boundary == 'partial_actions' else len(manifest.payload.actions) - 1
            if index == target:
                # _run_move's actual final phase, before the dedicated R2 hook.
                assert current._phase == 'IDENTITY_VERIFICATION'
                attempted(current)
            return result
        monkeypatch.setattr(sequence._ExecutionSequence, '_run_move', moved)
        with ScratchMaintenance(path, owner.root).maintenance() as lease:
            with pytest.raises(ScratchCoordinationStopped) as caught:
                owner.execute(manifest, maintenance_owner=lease,
                    completion_rescan=boundary != 'not_requested')
    error = caught.value
    assert error.reconciliation_required is True
    assert error.phase == 'P2C_COMPLETION'
    assert isinstance(error.__cause__, ValueError)
    assert owner.completion_rescan_result is None
    assert completion_state(owner, path) == reached['state']
    if boundary == 'partial_actions':
        assert len(owner._captures) == 1
        assert database_rows(path) != expected_rows(original, manifest.payload.locator_patches)
    else:
        assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
    assert reached['requested'] is (boundary not in {'cleanup', 'not_requested'})
    with pytest.raises(ScratchCoordinationStopped) as spent:
        owner.execute(manifest, completion_rescan=True)
    assert spent.value.reconciliation_required and spent.value.phase == error.phase
    assert completion_state(owner, path) == reached['state']


def test_bridge_on_fresh_unspent_owner_is_safe_rejection(case):
    owner, manifest, path = case
    before = completion_state(owner, path)
    with pytest.raises(ScratchCoordinationStopped) as caught:
        completion.run_completed_scratch_rescan(owner)
    assert type(caught.value) is ScratchCoordinationStopped
    assert caught.value.reconciliation_required is False
    assert not owner._spent and not owner._closed
    assert completion_state(owner, path) == before
