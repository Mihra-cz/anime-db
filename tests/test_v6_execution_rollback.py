"""Explicit one-shot inverse MOVE on original synthetic scratch artifacts."""
from dataclasses import replace
import gc
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

import pytest

from app import target_execution_filesystem as fs
from app import target_execution_rollback as rollback
from app.maintenance import ScratchMaintenance
from app.target_execution_coordination import ScratchActionSession
from app.target_execution_manifest import _digest, manifest_to_json
from test_v6_execution_database import sql
from test_v6_execution_reconciliation import environment, snapshot
from v6_database_case import prepare_database


@pytest.fixture
def case(tmp_path):
    with ScratchActionSession.create(tmp_path) as session:
        manifest, source, target, database = prepare_database(session)
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
        args = dict(action_index=0, scratch_root=session.root,
            journal_path=session.journal_path, evidence_path=session.evidence_path)
    gate = ScratchMaintenance(database, session.root)
    return manifest, source, target, database, args, gate


def proposal(case):
    manifest, _, _, _, args, _ = case
    before = snapshot(args['scratch_root'].parent)
    result = rollback.propose_scratch_video_rollback(manifest, manifest.payload.actions[0], **args)
    assert snapshot(args['scratch_root'].parent) == before
    return result


def approval(case):
    return rollback.RollbackApproval(proposal(case), 'RETURN_TARGET_TO_SOURCE',
        rollback.IDENTITY_LIMITATION, 'synthetic-test-user')


def execute(case, approved=None, owner=None):
    manifest, _, _, _, args, _ = case
    return rollback.rollback_scratch_video_move(manifest, manifest.payload.actions[0],
        **args, approval=approved, maintenance_owner=owner)


def evidence(case):
    root = case[4]['scratch_root']
    return root.parent / (root.name + '.rollback.jsonl')


def records(case):
    return [json.loads(line) for line in evidence(case).read_text().splitlines()]


def test_approved_rollback_preserves_db_original_journal_and_content(case, monkeypatch):
    _, source, target, db, args, gate = case
    approved = approval(case)
    assert approved.proposal.identity_continuity == 'UNKNOWN'
    assert 'inode' in approved.proposal.identity_limitation
    before = snapshot(source.parent.parent.parent)
    calls, barriers = [], []
    rename, sync = fs._rename_noreplace, os.fsync
    def renamed(*values):
        calls.append(values)
        assert [r['state'] for r in records(case)] == ['APPROVED', 'ATTEMPT']
        rename(*values)
    def synced(fd):
        barriers.append(os.readlink(f'/proc/self/fd/{fd}'))
        sync(fd)
    monkeypatch.setattr(fs, '_rename_noreplace', renamed)
    monkeypatch.setattr(os, 'fsync', synced)
    with gate.maintenance() as owner:
        result = execute(case, approved, owner)
    after = snapshot(source.parent.parent.parent)
    for path in (db, args['journal_path'], args['evidence_path']):
        key = str(path.relative_to(source.parent.parent.parent))
        assert after[key] == before[key]
    assert source.read_bytes() == b'original scratch video' and not target.exists()
    assert len(calls) == 1 and result.outcome == 'ROLLED_BACK'
    assert [r['state'] for r in records(case)] == ['APPROVED', 'ATTEMPT', 'VERIFIED']
    assert records(case)[0]['approval']['proposal'] == json.loads(json.dumps(approved.proposal.as_dict()))
    previous = None
    for sequence, frame in enumerate(records(case)):
        checksum = frame.pop('sha256')
        assert _digest(frame) == checksum and frame['previous_sha256'] == previous
        assert frame['sequence'] == sequence
        previous = checksum
    assert barriers[-4:-1] == [str(source), str(source.parent), str(target.parent)]


@pytest.mark.parametrize('fault', ['absent', 'decision', 'acknowledgement', 'digest'])
def test_missing_or_unbound_approval_never_writes(case, fault):
    approved = approval(case)
    if fault == 'absent':
        approved = None
    elif fault == 'digest':
        approved = replace(approved, proposal=replace(approved.proposal, proposal_sha256='0' * 64))
    else:
        approved = replace(approved, **{fault: 'not approved'})
    before = snapshot(case[4]['scratch_root'].parent)
    with case[-1].maintenance() as owner, pytest.raises(rollback.ScratchRollbackStopped):
        execute(case, approved, owner)
    assert snapshot(case[4]['scratch_root'].parent) == before


@pytest.mark.parametrize('fault', ['new_db', 'db_other_row', 'both_present', 'both_absent', 'content', 'evidence', 'manifest', 'index', 'sidecar'])
def test_invalid_current_state_stops_before_any_rollback_write(case, fault):
    manifest, source, target, database, args, gate = case
    approved = approval(case)
    if fault == 'new_db':
        sql(database, 'UPDATE videos SET relative_path=?,root_folder=? WHERE id=100',
            (manifest.payload.actions[0].target, 'Show'))
    elif fault == 'db_other_row':
        sql(database, "UPDATE videos SET filename='changed' WHERE id=200")
    elif fault == 'both_present':
        source.write_bytes(b'collision')
    elif fault == 'both_absent':
        target.unlink()
    elif fault == 'content':
        info = target.stat()
        target.write_bytes(b'x' * info.st_size)
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif fault == 'evidence':
        args['evidence_path'].write_bytes(b'wrong evidence\n')
    elif fault == 'manifest':
        manifest = replace(manifest, manifest_id='0' * 64)
        case = (manifest, source, target, database, args, gate)
    elif fault == 'index':
        args['action_index'] = 1
    else:
        database.with_name(database.name + '-wal').write_bytes(b'preserve sidecar')
    before = snapshot(args['scratch_root'].parent)
    with gate.maintenance() as owner, pytest.raises(rollback.ScratchRollbackStopped):
        execute(case, approved, owner)
    assert snapshot(args['scratch_root'].parent) == before
    assert not evidence(case).exists()


@pytest.mark.parametrize('owner_kind', ['missing', 'shared', 'closed'])
def test_exclusive_live_maintenance_is_mandatory(case, owner_kind):
    approved = approval(case)
    gate = case[-1]
    if owner_kind == 'shared':
        with gate.writer() as owner, pytest.raises(rollback.ScratchRollbackStopped):
            execute(case, approved, owner)
    else:
        owner = None
        if owner_kind == 'closed':
            with gate.maintenance() as owner:
                pass
        with pytest.raises(rollback.ScratchRollbackStopped):
            execute(case, approved, owner)
    assert not evidence(case).exists()


@pytest.mark.parametrize('fault', ['rejected', 'uncertain', 'interrupt', 'fsync', 'collision'])
def test_attempt_failure_preserves_actual_state_and_forbids_retry(case, monkeypatch, fault):
    _, source, target, db, args, gate = case
    approved = approval(case)
    saved = {p: p.read_bytes() for p in (db, args['journal_path'], args['evidence_path'])}
    rename, sync, calls = fs._rename_noreplace, os.fsync, []
    def attempted(*values):
        calls.append(values)
        if fault == 'rejected':
            raise PermissionError('injected rename failure')
        if fault == 'collision':
            source.write_bytes(b'concurrent collision')
        rename(*values)
        if fault == 'uncertain':
            raise OSError('lost syscall acknowledgement')
        if fault == 'interrupt':
            raise KeyboardInterrupt('interrupted after rename')
    def synced(fd):
        if fault == 'fsync' and calls:
            raise OSError('injected fsync uncertainty')
        sync(fd)
    monkeypatch.setattr(fs, '_rename_noreplace', attempted)
    monkeypatch.setattr(os, 'fsync', synced)
    gc.collect()
    before_fds = len(os.listdir('/proc/self/fd'))
    with gate.maintenance() as owner:
        with pytest.raises(KeyboardInterrupt if fault == 'interrupt' else rollback.ScratchRollbackStopped) as stopped:
            execute(case, approved, owner)
        if fault != 'interrupt':
            assert stopped.value.reconciliation_required is True and stopped.value.__cause__ is not None
        with pytest.raises(rollback.ScratchRollbackStopped):
            execute(case, approved, owner)
    gc.collect()
    assert len(os.listdir('/proc/self/fd')) == before_fds
    assert len(calls) == 1
    assert all(p.read_bytes() == raw for p, raw in saved.items())
    if fault == 'rejected':
        assert not source.exists() and target.read_bytes() == b'original scratch video'
    elif fault == 'collision':
        assert source.read_bytes() == b'concurrent collision' and target.exists()
    else:
        assert source.read_bytes() == b'original scratch video' and not target.exists()
    assert [r['state'] for r in records(case)][:2] == ['APPROVED', 'ATTEMPT']
    assert all(r['state'] != 'VERIFIED' for r in records(case))


def test_rejected_and_successful_paths_release_descriptors(case):
    approved = approval(case)
    gc.collect()
    before = len(os.listdir('/proc/self/fd'))
    with case[-1].maintenance() as owner:
        for _ in range(3):
            with pytest.raises(rollback.ScratchRollbackStopped):
                execute(case, None, owner)
        execute(case, approved, owner)
    gc.collect()
    assert len(os.listdir('/proc/self/fd')) == before


def test_foreign_exclusive_owner_cannot_authorize_this_database(case):
    approved = approval(case)
    root = case[4]['scratch_root']
    foreign = ScratchMaintenance(root.parent / 'other.sqlite', root)
    with foreign.maintenance() as owner, pytest.raises(rollback.ScratchRollbackStopped):
        execute(case, approved, owner)
    assert not evidence(case).exists()


@pytest.mark.parametrize('fault', ['inode', 'hardlink', 'symlink', 'permissions', 'mtime'])
def test_hash_equality_does_not_override_unsafe_or_changed_metadata(case, fault):
    approved = approval(case)
    target = case[2]
    before = target.stat()
    if fault in {'inode', 'symlink'}:
        original = target.with_suffix('.saved')
        target.rename(original)
        if fault == 'inode':
            target.write_bytes(original.read_bytes())
            target.chmod(0o600)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        else:
            target.symlink_to(original)
    elif fault == 'hardlink':
        os.link(target, target.with_suffix('.link'))
    elif fault == 'permissions':
        target.chmod(0o666)
    else:
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
    with case[-1].maintenance() as owner, pytest.raises(rollback.ScratchRollbackStopped):
        execute(case, approved, owner)
    assert not evidence(case).exists()


@pytest.mark.parametrize('boundary', ['APPROVED', 'ATTEMPT'])
def test_changes_during_durable_evidence_cannot_reach_rename(case, monkeypatch, boundary):
    approved = approval(case)
    sync = os.fsync
    changed, calls = [], []
    def synced(fd):
        sync(fd)
        if evidence(case).exists() and not changed:
            frames = records(case)
            if frames and frames[-1]['state'] == boundary:
                changed.append(True)
                case[1].write_bytes(b'collision after approval')
    monkeypatch.setattr(os, 'fsync', synced)
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *args: calls.append(args))
    with case[-1].maintenance() as owner, pytest.raises(rollback.ScratchRollbackStopped):
        execute(case, approved, owner)
    assert changed and not calls and case[2].exists()
    assert all(r['state'] != 'VERIFIED' for r in records(case))


def test_partial_existing_evidence_never_reopens_or_retries(case, monkeypatch):
    approved = approval(case)
    evidence(case).write_bytes(b'partial prior attempt')
    before = snapshot(case[4]['scratch_root'].parent)
    def forbidden(*args):
        pytest.fail('retried prior rollback')
    monkeypatch.setattr(fs, '_rename_noreplace', forbidden)
    with case[-1].maintenance() as owner, pytest.raises(rollback.ScratchRollbackStopped):
        execute(case, approved, owner)
    assert snapshot(case[4]['scratch_root'].parent) == before


@pytest.mark.parametrize('frame', ['APPROVED', 'ATTEMPT', 'VERIFIED'])
def test_evidence_fsync_failure_never_reports_success_or_retries(case, monkeypatch, frame):
    approved = approval(case)
    sync, rename, calls = os.fsync, fs._rename_noreplace, []
    def renamed(*args):
        calls.append(args)
        rename(*args)
    def synced(fd):
        if os.readlink(f'/proc/self/fd/{fd}') == str(evidence(case)):
            if records(case)[-1]['state'] == frame:
                raise OSError('evidence fsync acknowledgement lost')
        sync(fd)
    monkeypatch.setattr(os, 'fsync', synced)
    monkeypatch.setattr(fs, '_rename_noreplace', renamed)
    before_fds = len(os.listdir('/proc/self/fd'))
    with case[-1].maintenance() as owner:
        with pytest.raises(rollback.ScratchRollbackStopped) as stopped:
            execute(case, approved, owner)
        assert stopped.value.reconciliation_required is (frame == 'VERIFIED')
        raw = evidence(case).read_bytes()
        with pytest.raises(rollback.ScratchRollbackStopped):
            execute(case, approved, owner)
        assert evidence(case).read_bytes() == raw
    assert len(calls) == (1 if frame == 'VERIFIED' else 0)
    assert case[1].exists() is (frame == 'VERIFIED')
    assert case[2].exists() is (frame != 'VERIFIED')
    assert len(os.listdir('/proc/self/fd')) == before_fds


def test_rollback_evidence_tampering_stops_before_rename(case, monkeypatch):
    approved = approval(case)
    sync, changed = os.fsync, []
    def synced(fd):
        sync(fd)
        if evidence(case).exists() and not changed:
            changed.append(True)
            with evidence(case).open('ab') as stream:
                stream.write(b'foreign bytes\n')
    monkeypatch.setattr(os, 'fsync', synced)
    def forbidden(*args):
        pytest.fail('rename after rollback evidence was altered')
    monkeypatch.setattr(fs, '_rename_noreplace', forbidden)
    with case[-1].maintenance() as owner, pytest.raises(rollback.ScratchRollbackStopped):
        execute(case, approved, owner)
    assert changed and not case[1].exists() and case[2].exists()


def test_change_during_final_evidence_never_returns_false_success(case, monkeypatch):
    approved = approval(case)
    sync, changed = os.fsync, []
    def synced(fd):
        sync(fd)
        if evidence(case).exists() and not changed and records(case)[-1]['state'] == 'VERIFIED':
            changed.append(True)
            info, raw = case[1].stat(), case[1].read_bytes()
            case[1].write_bytes(raw)
            os.utime(case[1], ns=(info.st_atime_ns, info.st_mtime_ns))
    monkeypatch.setattr(os, 'fsync', synced)
    with case[-1].maintenance() as owner, pytest.raises(rollback.ScratchRollbackStopped) as stopped:
        execute(case, approved, owner)
    assert changed and stopped.value.reconciliation_required is True
    assert stopped.value.phase == 'RESULT_EVIDENCE'
    assert case[1].read_bytes() == b'original scratch video' and not case[2].exists()


def test_action_argument_must_exactly_match_the_sealed_action(case):
    approved = approval(case)
    manifest, _, _, _, args, gate = case
    wrong = replace(manifest.payload.actions[0], object_id=200)
    with gate.maintenance() as owner, pytest.raises(rollback.ScratchRollbackStopped):
        rollback.rollback_scratch_video_move(manifest, wrong, **args,
            approval=approved, maintenance_owner=owner)
    assert not evidence(case).exists()


def test_active_original_writer_is_never_adopted_or_modified(tmp_path, monkeypatch):
    from app.target_execution_journal import DurableJournal
    append, checked = DurableJournal.append, []
    with ScratchActionSession.create(tmp_path) as session:
        manifest, source, target, database = prepare_database(session)
        def appended(writer, entry):
            append(writer, entry)
            if entry.state == 'FS_VERIFIED':
                before = snapshot(tmp_path)
                with pytest.raises(rollback.ScratchRollbackStopped):
                    rollback.propose_scratch_video_rollback(manifest, manifest.payload.actions[0],
                        action_index=0, scratch_root=session.root, journal_path=session.journal_path,
                        evidence_path=session.evidence_path)
                assert snapshot(tmp_path) == before
                checked.append(True)
        monkeypatch.setattr(DurableJournal, 'append', appended)
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
        assert checked == [True]


def test_foreign_root_is_rejected_without_production_path_observation(case, monkeypatch):
    approved = approval(case)
    original = fs._open_absolute
    def opened(path):
        assert str(path).startswith('/tmp/'), 'observed non-scratch path'
        return original(path)
    monkeypatch.setattr(fs, '_open_absolute', opened)
    case[4]['scratch_root'] = Path('/mnt/forbidden-production-library')
    with pytest.raises(rollback.ScratchRollbackStopped):
        execute(case, approved, None)


def test_actual_original_process_death_then_approved_rollback_in_new_process(tmp_path):
    import test_v6_execution_database_restart as processes
    ready = processes.killed(tmp_path, 'after_fs_verified')
    helper = Path(__file__).with_name('v6_rollback_helper.py')
    original = {name: Path(ready['paths'][name]).read_bytes() for name in ('database', 'journal', 'evidence')}
    child = subprocess.run([sys.executable, '-Werror::ResourceWarning', str(helper),
        ready['paths']['manifest'], 'success'], env=environment(), capture_output=True, text=True, timeout=30)
    assert child.returncode == 0 and not child.stderr, child.stdout + child.stderr
    done = json.loads(child.stdout)
    assert done['assertions_completed'] is True and done['outcome'] == 'ROLLED_BACK'
    assert done['pid'] not in {os.getpid(), ready['pid']}
    assert all(Path(ready['paths'][name]).read_bytes() == raw for name, raw in original.items())


@pytest.mark.parametrize('boundary', ['before_rename', 'after_rename'])
def test_sigkill_during_rollback_releases_lock_but_never_allows_automatic_retry(case, boundary):
    manifest_path = case[4]['scratch_root'].parent / 'manifest.json'
    manifest_path.write_text(manifest_to_json(case[0]))
    helper = Path(__file__).with_name('v6_rollback_helper.py')
    approved = approval(case)
    original = {p: p.read_bytes() for p in (case[3], case[4]['journal_path'], case[4]['evidence_path'])}
    child = subprocess.Popen([sys.executable, '-Werror::ResourceWarning', str(helper),
        str(manifest_path), boundary], env=environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([child.stdout], [], [], 30)[0], 'rollback child never reached boundary'
        line = child.stdout.readline()
        assert line, child.stderr.read()
        ready = json.loads(line)
        assert ready['assertions_completed'] is True and ready['boundary'] == boundary
        assert ready['pid'] == child.pid and child.poll() is None
        child.kill()
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL and not stdout and not stderr
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
    assert case[1].exists() is (boundary == 'after_rename')
    assert case[2].exists() is (boundary == 'before_rename')
    assert all(p.read_bytes() == raw for p, raw in original.items())
    before = snapshot(case[4]['scratch_root'].parent)
    with case[-1].maintenance() as owner, pytest.raises(rollback.ScratchRollbackStopped):
        execute(case, approved, owner)
    assert snapshot(case[4]['scratch_root'].parent) == before
    assert [r['state'] for r in records(case)] == ['APPROVED', 'ATTEMPT']
