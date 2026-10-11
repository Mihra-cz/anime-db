"""E3B real scratch coordination; faults are not NAS/power-loss capability proof."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import threading

import pytest

from app import target_execution_coordination as coordination
from app import target_execution_filesystem as fs
from app import target_execution_identity as identity
from app import target_execution_journal as journal
from app.target_execution_manifest import manifest_to_json
from test_v6_execution_filesystem import prepare, seal


@pytest.fixture
def case(tmp_path):
    with coordination.ScratchActionSession.create(tmp_path) as session:
        yield session, *prepare(session)


def execute(session, manifest):
    return session.execute(manifest, manifest.payload.actions[0], action_index=0)


def evidence_path(session):
    return session.root.parent / (session.root.name + '.identity.json')


def states(session, manifest):
    if not session.journal_path.exists():
        return []
    return [entry.state for entry in journal.load_journal(session.journal_path, manifest).entries]


def rewrite_json(path, change):
    """Deliberate test tampering: produce strict, correctly checksummed bytes."""
    frame = json.loads(path.read_bytes())
    change(frame['evidence'])
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    frame['sha256'] = hashlib.sha256(canonical(frame['evidence']).encode()).hexdigest()
    path.write_bytes((canonical(frame) + '\n').encode())


@pytest.mark.parametrize('action', ['MOVE', 'QUARANTINE'])
def test_single_owned_root_produces_bound_evidence_and_verified_history(tmp_path, monkeypatch, action):
    create, close = fs.ScratchRenameSession.create, fs.ScratchRenameSession.close
    allocations, closes = [], []
    def allocated(cls, parent):
        session = create(parent)
        allocations.append(session)
        return session
    def closed(session):
        closes.append(session)
        return close(session)
    monkeypatch.setattr(fs.ScratchRenameSession, 'create', classmethod(allocated))
    monkeypatch.setattr(fs.ScratchRenameSession, 'close', closed)
    with coordination.ScratchActionSession.create(tmp_path) as session:
        manifest, source, target = prepare(session, action=action)
        before, immutable = source.stat(), manifest_to_json(manifest)
        outcome = execute(session, manifest)
        path = evidence_path(session)
        assert path.exists(), 'rename completed without mandatory durable evidence'
        record = session._identity.load(manifest, manifest.payload.actions[0], action_index=0)
        assert record.action == manifest.payload.actions[0] and record.action_index == 0
        assert record.manifest_sha256 == hashlib.sha256(immutable.encode()).hexdigest()
        assert record.binding.root == str(session.root)
        assert record.filesystem.st_ino == outcome.verification.inode == before.st_ino
        assert record.content.digest == hashlib.sha256(target.read_bytes()).hexdigest()
        assert session.evidence_path == path
        assert session._identity._filesystem is session._filesystem is allocations[0]
        assert len(allocations) == 1 and closes == allocations
        assert not source.exists() and target.exists()
        assert states(session, manifest) == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED']
        assert outcome.outcome == 'FS_VERIFIED' and manifest_to_json(manifest) == immutable
    assert closes == allocations  # the borrowed filesystem is never closed twice


def test_evidence_pending_rename_and_verification_barriers_are_ordered(case, monkeypatch):
    session, manifest, source, target = case
    path, events = evidence_path(session), []
    capture, compare = identity.ScratchIdentitySession.capture, identity.ScratchIdentitySession.compare
    sync, append = os.fsync, journal.DurableJournal.append
    rename, verify = fs._rename_noreplace, fs.ScratchObservation.verify
    labels = {str(path): 'evidence', str(session.root.parent): 'parent', str(session.journal_path): 'journal',
        str(target): 'target', str(target.parent): 'target-parent', str(source.parent): 'source-parent'}
    def captured(owner, *args, **kwargs):
        result = capture(owner, *args, **kwargs)
        events.append('capture-ack')
        return result
    def compared(owner, *args, **kwargs):
        result = compare(owner, *args, **kwargs)
        events.append('identity-after' if not source.exists() else 'identity-before')
        return result
    def synced(fd):
        sync(fd)
        events.append(labels[os.readlink(f'/proc/self/fd/{fd}')] + '-fsync')
    def appended(writer, entry):
        assert path.is_file(), 'journal advanced without identity evidence'
        if entry.state == 'PENDING':
            assert 'capture-ack' in events and events.index('evidence-fsync') < events.index('capture-ack')
        append(writer, entry)
        events.append(entry.state)
    def renamed(*args):
        assert 'PENDING' in events and events[-1] == 'identity-before'
        events.append('rename')
        return rename(*args)
    def verified(observation):
        result = verify(observation)
        events.append('independent-verification')
        return result
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture', captured)
    monkeypatch.setattr(identity.ScratchIdentitySession, 'compare', compared)
    monkeypatch.setattr(os, 'fsync', synced)
    monkeypatch.setattr(journal.DurableJournal, 'append', appended)
    monkeypatch.setattr(fs, '_rename_noreplace', renamed)
    monkeypatch.setattr(fs.ScratchObservation, 'verify', verified)
    execute(session, manifest)
    assert events == ['evidence-fsync', 'parent-fsync', 'capture-ack', 'identity-before',
        'journal-fsync', 'parent-fsync', 'journal-fsync', 'PENDING', 'identity-before', 'rename',
        'target-fsync', 'target-parent-fsync', 'source-parent-fsync', 'journal-fsync', 'FS_APPLIED',
        'independent-verification', 'identity-after', 'journal-fsync', 'FS_VERIFIED']


def test_capture_failure_cannot_start_pending_or_rename(case, monkeypatch):
    session, manifest, source, target = case
    def failed(*args, **kwargs):
        raise identity.IdentityEvidenceReconciliationRequired('unacknowledged evidence write')
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture', failed)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == 'EVIDENCE_CAPTURE'
    assert not session.journal_path.exists() and source.exists() and not target.exists()


@pytest.mark.parametrize('when', ['PENDING', 'FS_APPLIED'])
def test_full_hash_detects_changed_bytes_with_identical_size_and_mtime(case, monkeypatch, when):
    session, manifest, source, target = case
    append = journal.DurableJournal.append
    def mutate(writer, entry):
        append(writer, entry)
        if entry.state == when:
            path = source if when == 'PENDING' else target
            info = path.stat()
            path.write_bytes(b'X' * info.st_size)
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    monkeypatch.setattr(journal.DurableJournal, 'append', mutate)
    with pytest.raises(coordination.ScratchCoordinationStopped) as error:
        execute(session, manifest)
    assert error.value.phase == ('PRE_RENAME_IDENTITY' if when == 'PENDING' else 'IDENTITY_VERIFICATION')
    assert states(session, manifest) == (['PENDING'] if when == 'PENDING' else ['PENDING', 'FS_APPLIED'])
    assert source.exists() is (when == 'PENDING') and target.exists() is (when == 'FS_APPLIED')


def test_valid_rechecksummed_evidence_cannot_replace_acknowledged_capture(case, monkeypatch):
    session, manifest, source, target = case
    append = journal.DurableJournal.append
    def tamper(writer, entry):
        append(writer, entry)
        if entry.state == 'FS_APPLIED':
            info = target.stat()
            target.write_bytes(b'X' * info.st_size)
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            rewrite_json(evidence_path(session), lambda body: body['content'].update(
                digest=hashlib.sha256(target.read_bytes()).hexdigest()))
    monkeypatch.setattr(journal.DurableJournal, 'append', tamper)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired):
        execute(session, manifest)
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED']
    assert not source.exists() and target.read_bytes() == b'X' * 22


@pytest.mark.parametrize('phase', ['before_write', 'partial_write', 'file_fsync', 'parent_fsync'])
def test_real_capture_io_failure_preserves_evidence_and_never_creates_journal(case, monkeypatch, phase):
    session, manifest, source, target = case
    sync = os.fsync
    calls = []
    def failed_write(fd, raw):
        calls.append('write')
        if phase == 'partial_write':
            os.write(fd, raw[:37])
        raise OSError(errno.EIO, 'capture write')
    def failed_sync(fd):
        path = os.readlink(f'/proc/self/fd/{fd}')
        if (phase, path) in {('file_fsync', str(evidence_path(session))), ('parent_fsync', str(session.root.parent))}:
            calls.append('fsync')
            raise OSError(errno.EIO, 'capture barrier')
        sync(fd)
    if phase.endswith('write'):
        monkeypatch.setattr(identity, '_write_record', failed_write)
    else:
        monkeypatch.setattr(os, 'fsync', failed_sync)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == 'EVIDENCE_CAPTURE'
    raw = evidence_path(session).read_bytes()
    assert len(raw) == 0 if phase == 'before_write' else len(raw) == 37 if phase == 'partial_write' else len(raw) > 37
    assert not session.journal_path.exists() and source.exists() and not target.exists()
    with pytest.raises(type(error.value)) as repeated:
        execute(session, manifest)
    assert repeated.value.phase == 'EVIDENCE_CAPTURE' and len(calls) == 1
    assert evidence_path(session).read_bytes() == raw


@pytest.mark.parametrize('state,expected,moved', [
    ('PENDING', [], False), ('FS_APPLIED', ['PENDING'], True),
    ('FS_VERIFIED', ['PENDING', 'FS_APPLIED'], True),
])
def test_journal_failure_retains_evidence_and_exact_phase(case, monkeypatch, state, expected, moved):
    session, manifest, source, target = case
    append, calls = journal.DurableJournal.append, []
    def fail(writer, entry):
        calls.append(entry.state)
        if entry.state == state:
            raise OSError(errno.EIO, 'journal durability')
        return append(writer, entry)
    monkeypatch.setattr(journal.DurableJournal, 'append', fail)
    with pytest.raises(coordination.ScratchJournalReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == state and states(session, manifest) == expected
    assert source.exists() is not moved and target.exists() is moved
    raw = evidence_path(session).read_bytes()
    acknowledged = tuple(calls)
    with pytest.raises(coordination.ScratchJournalReconciliationRequired) as repeated:
        execute(session, manifest)
    assert repeated.value.phase == state and tuple(calls) == acknowledged
    assert evidence_path(session).read_bytes() == raw


@pytest.mark.parametrize('when', ['capture', 'PENDING', 'FS_APPLIED'])
@pytest.mark.parametrize('change', ['checksum', 'manifest', 'root', 'digest', 'inode', 'replacement'])
def test_untrusted_evidence_stops_without_further_transitions(case, monkeypatch, when, change):
    session, manifest, source, target = case
    capture, append = identity.ScratchIdentitySession.capture, journal.DurableJournal.append
    def tamper():
        path = evidence_path(session)
        if change == 'checksum':
            frame = json.loads(path.read_bytes())
            frame['sha256'] = '0' * 64
            path.write_text(json.dumps(frame) + '\n')
        elif change == 'replacement':
            raw = path.read_bytes()
            path.rename(path.with_suffix('.original'))
            path.write_bytes(raw)
            path.chmod(0o600)
        else:
            def mutate(body):
                if change == 'manifest': body['manifest_id'] = '0' * 64
                elif change == 'root': body['binding']['root'] = str(session.root.parent / 'other-root')
                elif change == 'digest': body['content']['digest'] = '0' * 64
                else: body['filesystem']['st_ino'] += 1
            rewrite_json(path, mutate)
    def captured(owner, *args, **kwargs):
        result = capture(owner, *args, **kwargs)
        if when == 'capture': tamper()
        return result
    def appended(writer, entry):
        append(writer, entry)
        if entry.state == when: tamper()
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture', captured)
    monkeypatch.setattr(journal.DurableJournal, 'append', appended)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == {'capture': 'EVIDENCE_VERIFY', 'PENDING': 'PENDING',
        'FS_APPLIED': 'FS_APPLIED'}[when]
    assert states(session, manifest) == {'capture': [], 'PENDING': ['PENDING'],
        'FS_APPLIED': ['PENDING', 'FS_APPLIED']}[when]
    assert source.exists() is (when != 'FS_APPLIED') and target.exists() is (when == 'FS_APPLIED')


def test_corrupt_evidence_after_journal_header_does_not_append_pending(case, monkeypatch):
    session, manifest, source, target = case
    create = journal.DurableJournal.create
    def created(cls, *args, **kwargs):
        writer = create(*args, **kwargs)
        evidence_path(session).write_bytes(b'broken evidence\n')
        return writer
    monkeypatch.setattr(journal.DurableJournal, 'create', classmethod(created))
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == 'PENDING' and states(session, manifest) == []
    assert source.exists() and not target.exists()


def test_evidence_changed_during_final_append_cannot_return_verified_outcome(case, monkeypatch):
    session, manifest, source, target = case
    append = journal.DurableJournal.append
    def corrupt(writer, entry):
        append(writer, entry)
        if entry.state == 'FS_VERIFIED':
            evidence_path(session).write_bytes(b'corrupted after journal acknowledgement\n')
    monkeypatch.setattr(journal.DurableJournal, 'append', corrupt)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == 'FS_VERIFIED'
    # Durable history is diagnostic evidence and must never be repaired/deleted.
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED']
    assert not source.exists() and target.exists()


@pytest.mark.parametrize('change', ['inode', 'size', 'mtime', 'symlink', 'parent', 'both', 'neither', 'already_moved'])
def test_changed_source_or_ambiguous_locators_after_pending_never_rename(case, monkeypatch, change):
    session, manifest, source, target = case
    append, renames = journal.DurableJournal.append, []
    def mutate(writer, entry):
        append(writer, entry)
        if entry.state != 'PENDING': return
        info = source.stat()
        if change in {'inode', 'symlink'}:
            original = source.with_suffix('.original')
            source.rename(original)
            if change == 'inode':
                source.write_bytes(original.read_bytes())
                source.chmod(0o600)
                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
            else: source.symlink_to(original)
        elif change == 'size': source.write_bytes(b'longer changed scratch file')
        elif change == 'mtime': os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        elif change == 'parent': source.parent.rename(session.root / 'displaced'); source.parent.mkdir(mode=0o700)
        elif change == 'both': target.write_bytes(source.read_bytes()); target.chmod(0o600)
        elif change == 'already_moved': source.rename(target)
        else: source.unlink()
    def attempted(*args):
        renames.append(1)
        pytest.fail('changed source renamed')
    monkeypatch.setattr(journal.DurableJournal, 'append', mutate)
    monkeypatch.setattr(fs, '_rename_noreplace', attempted)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == 'PRE_RENAME_IDENTITY' and renames == []
    assert states(session, manifest) == ['PENDING']


@pytest.mark.parametrize('kind', ['UNKNOWN', 'MISMATCH', 'reversed', 'unobserved', 'raised'])
def test_post_rename_comparison_must_supply_real_unique_matching_observations(case, monkeypatch, kind):
    session, manifest, source, target = case
    compare = identity.ScratchIdentitySession.compare
    def unreliable(owner, *args, **kwargs):
        result = compare(owner, *args, **kwargs)
        if source.exists(): return result
        if kind == 'raised': raise OSError(errno.EIO, 'post-rename comparison')
        if kind == 'reversed': return replace(result, source=result.target, target=result.source)
        if kind == 'unobserved': return replace(result, target=replace(result.target, observed_file=None))
        return replace(result, outcome=kind)
    monkeypatch.setattr(identity.ScratchIdentitySession, 'compare', unreliable)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == 'IDENTITY_VERIFICATION'
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED']
    assert not source.exists() and target.exists()


@pytest.mark.parametrize('change', ['both', 'neither'])
def test_new_real_observation_after_independent_verification_is_required(case, monkeypatch, change):
    session, manifest, source, target = case
    verify = fs.ScratchObservation.verify
    def changed_after_verification(observation):
        result = verify(observation)
        if change == 'both':
            source.write_bytes(target.read_bytes())
            source.chmod(0o600)
        else: target.unlink()
        return result
    monkeypatch.setattr(fs.ScratchObservation, 'verify', changed_after_verification)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == 'IDENTITY_VERIFICATION'
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED']


def test_evidence_corruption_after_durable_rename_prevents_fs_applied(case, monkeypatch):
    session, manifest, source, target = case
    rename = fs.ScratchRenameSession.rename
    def corrupted_after_rename(owner, *args, **kwargs):
        result = rename(owner, *args, **kwargs)
        evidence_path(session).write_bytes(b'changed after durable rename\n')
        return result
    monkeypatch.setattr(fs.ScratchRenameSession, 'rename', corrupted_after_rename)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == 'FS_APPLIED'
    assert states(session, manifest) == ['PENDING']
    assert not source.exists() and target.exists()


def test_whole_evidence_from_another_real_manifest_and_root_is_refused(case, tmp_path, monkeypatch):
    session, manifest, source, target = case
    with identity.ScratchIdentitySession.create(tmp_path) as foreign:
        other, _, _ = prepare(foreign)
        foreign.capture(other, other.payload.actions[0], action_index=0)
        raw = foreign.evidence_path.read_bytes()
    capture = identity.ScratchIdentitySession.capture
    def substituted(owner, *args, **kwargs):
        result = capture(owner, *args, **kwargs)
        evidence_path(session).write_bytes(raw)
        return result
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture', substituted)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == 'EVIDENCE_VERIFY'
    assert not session.journal_path.exists() and source.exists() and not target.exists()
    assert evidence_path(session).read_bytes() == raw


@pytest.mark.parametrize('pin', ['artifact', 'parent'])
def test_capture_pin_allocation_failure_cleans_up_without_pending(case, monkeypatch, pin):
    session, manifest, source, target = case
    open_fd, duplicate = os.open, os.dup
    attempted = []
    def fail_open(path, flags, *args, **kwargs):
        if path == evidence_path(session).name and flags == fs._FILE:
            attempted.append(1)
            raise OSError(errno.EMFILE, 'artifact read pin')
        return open_fd(path, flags, *args, **kwargs)
    def fail_dup(fd):
        if os.readlink(f'/proc/self/fd/{fd}') == str(session.root.parent):
            attempted.append(1)
            raise OSError(errno.EMFILE, 'artifact parent pin')
        return duplicate(fd)
    with monkeypatch.context() as injected:
        if pin == 'artifact': injected.setattr(os, 'open', fail_open)
        else: injected.setattr(os, 'dup', fail_dup)
        with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
            execute(session, manifest)
    assert error.value.phase == 'EVIDENCE_CAPTURE' and attempted == [1]
    assert evidence_path(session).exists() and not session.journal_path.exists()
    assert source.exists() and not target.exists()
    assert session._identity._closed and session._filesystem._closed
    assert not any(str(evidence_path(session)) == os.readlink(p) for p in Path('/proc/self/fd').iterdir() if p.exists())


def test_missing_capture_acknowledgement_cannot_be_replaced_by_a_valid_record(case, monkeypatch):
    session, manifest, source, target = case
    capture = identity.ScratchIdentitySession.capture
    def lost_ack(owner, *args, **kwargs):
        result = capture(owner, *args, **kwargs)
        owner._durable = False  # explicit fault: parseable bytes without live acknowledgement
        return result
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture', lost_ack)
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as error:
        execute(session, manifest)
    assert error.value.phase == 'EVIDENCE_VERIFY'
    assert evidence_path(session).is_file() and not session.journal_path.exists()
    assert source.exists() and not target.exists()


@pytest.mark.parametrize('signal_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('phase', ['capture', 'evidence_verify', 'pre_rename', 'post_rename'])
def test_identity_interruptions_preserve_original_signal_code_and_stop(case, monkeypatch, signal_type, phase):
    session, manifest, source, target = case
    signal = signal_type(154)
    compare, calls = identity.ScratchIdentitySession.compare, []
    def capture_interrupt(*args):
        calls.append(1)
        raise signal
    def comparison_interrupt(owner, *args, **kwargs):
        calls.append(1)
        selected = {'evidence_verify': 1, 'pre_rename': 2, 'post_rename': 3}[phase]
        if len(calls) == selected: raise signal
        return compare(owner, *args, **kwargs)
    if phase == 'capture': monkeypatch.setattr(identity, '_write_record', capture_interrupt)
    else: monkeypatch.setattr(identity.ScratchIdentitySession, 'compare', comparison_interrupt)
    with pytest.raises(signal_type) as error:
        execute(session, manifest)
    assert error.value is signal and signal.__traceback__ is not None
    if signal_type is SystemExit: assert signal.code == 154
    expected = {'capture': 'EVIDENCE_CAPTURE', 'evidence_verify': 'EVIDENCE_VERIFY',
        'pre_rename': 'PRE_RENAME_IDENTITY', 'post_rename': 'IDENTITY_VERIFICATION'}[phase]
    assert session._spent and session._closed and session._identity._closed and session._filesystem._closed
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as repeated:
        execute(session, manifest)
    assert repeated.value.phase == expected
    assert len(calls) == {'capture': 1, 'evidence_verify': 1, 'pre_rename': 2, 'post_rename': 3}[phase]
    assert states(session, manifest) == {'capture': [], 'evidence_verify': [],
        'pre_rename': ['PENDING'], 'post_rename': ['PENDING', 'FS_APPLIED']}[phase]


def test_concurrent_execute_uses_one_capture_one_rename_and_one_history(case, monkeypatch):
    session, manifest, source, target = case
    capture, rename, starts = identity.ScratchIdentitySession.capture, fs._rename_noreplace, threading.Barrier(2)
    captures, renames = [], []
    def captured(owner, *args, **kwargs):
        captures.append(owner)
        return capture(owner, *args, **kwargs)
    def renamed(*args):
        renames.append(1)
        return rename(*args)
    def attempt():
        starts.wait(timeout=10)
        try: return execute(session, manifest).outcome
        except coordination.ScratchCoordinationStopped: return 'STOP'
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture', captured)
    monkeypatch.setattr(fs, '_rename_noreplace', renamed)
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(lambda _: attempt(), range(2)))
    assert sorted(outcomes) == ['FS_VERIFIED', 'STOP']
    assert captures == [session._identity] and renames == [1]
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED']
    assert not source.exists() and target.exists()


def test_factory_failure_releases_its_single_identity_owned_context(tmp_path, monkeypatch):
    lock, close = threading.Lock, fs.ScratchRenameSession.close
    locks, closed = [], []
    before = {p.name for p in Path('/proc/self/fd').iterdir()}
    primary = OSError(errno.EIO, 'coordination mutex allocation')
    def allocate_lock():
        locks.append(1)
        if len(locks) == 3: raise primary
        return lock()
    def closed_fs(owner):
        closed.append(owner)
        close(owner)
    with monkeypatch.context() as injected:
        injected.setattr(threading, 'Lock', allocate_lock)
        injected.setattr(fs.ScratchRenameSession, 'close', closed_fs)
        with pytest.raises(OSError) as error:
            coordination.ScratchActionSession.create(tmp_path)
    assert error.value is primary and len(closed) == 1 and closed[0]._closed
    assert {p.name for p in Path('/proc/self/fd').iterdir()} == before
    assert len(list(tmp_path.glob('v6-e2a-*'))) == 1


@pytest.mark.parametrize('primary_kind', ['io', 'keyboard', 'exit'])
@pytest.mark.parametrize('cleanup_kind', ['io', 'keyboard', 'exit'])
def test_shared_resource_cleanup_keeps_precedence_phase_and_closes_each_fd_once(case, monkeypatch, primary_kind, cleanup_kind):
    session, manifest, source, target = case
    original = OSError(errno.EIO, 'pending STOP') if primary_kind == 'io' else KeyboardInterrupt('pending interrupt') if primary_kind == 'keyboard' else SystemExit(141)
    cleanup = OSError(errno.EIO, 'identity cleanup') if cleanup_kind == 'io' else KeyboardInterrupt('cleanup interrupt') if cleanup_kind == 'keyboard' else SystemExit(175)
    capture, close = identity.ScratchIdentitySession.capture, os.close
    finish, observed, watched, closed = session._finish, [], {}, []
    def captured(owner, *args, **kwargs):
        result = capture(owner, *args, **kwargs)
        for name in ('_pin_fd', '_source_parent_fd', '_target_parent_fd', '_artifact_fd', '_artifact_parent_fd'):
            watched[getattr(owner, name)] = name
        watched[owner._filesystem._root_fd] = 'root'
        return result
    def failed_append(writer, entry):
        watched[writer._fd], watched[writer._parent_fd] = 'journal', 'journal-parent'
        raise original
    def closed_fd(fd):
        label = watched.pop(fd, None)  # only still-owned descriptors are fault targets
        close(fd)
        if label:
            closed.append(label)
        if label == '_artifact_fd': raise cleanup
    def finished(writer, primary):
        observed.append(primary)
        finish(writer, primary)
    with monkeypatch.context() as injected:
        injected.setattr(identity.ScratchIdentitySession, 'capture', captured)
        injected.setattr(journal.DurableJournal, 'append', failed_append)
        injected.setattr(os, 'close', closed_fd)
        injected.setattr(session, '_finish', finished)
        with pytest.raises(BaseException) as error:
            execute(session, manifest)
    primary = observed[0]
    expected = original if primary_kind != 'io' else cleanup if cleanup_kind != 'io' else primary
    assert error.value is expected and expected.__traceback__ is not None
    if isinstance(expected, SystemExit): assert expected.code == (141 if expected is original else 175)
    if expected is cleanup: assert cleanup.__cause__ is primary
    else: assert any('cleanup' in note for note in expected.__notes__)
    assert sorted(closed) == sorted(['_pin_fd', '_source_parent_fd', '_target_parent_fd', '_artifact_fd',
        '_artifact_parent_fd', 'root', 'journal', 'journal-parent']) and not watched
    assert session._identity._closed and session._filesystem._closed
    with pytest.raises(coordination.ScratchJournalReconciliationRequired) as repeated:
        execute(session, manifest)
    assert repeated.value.phase == 'PENDING'
    assert states(session, manifest) == [] and source.exists() and not target.exists()


@pytest.mark.parametrize('state', ['PENDING', 'FS_APPLIED', 'FS_VERIFIED'])
@pytest.mark.parametrize('signal_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('position', ['before', 'after'])
def test_interrupted_identity_anchor_keeps_fs_category_at_journal_boundary(case, monkeypatch, state, signal_type, position):
    session, manifest, source, target = case
    signal = signal_type(187)
    gate = session._check_identity_anchor
    calls = 0
    def interrupted(owner, *args, **kwargs):
        raise signal
    def anchor(*args):
        nonlocal calls
        if args[-1] == state:
            calls += 1
            if calls == (1 if position == 'before' else 2):
                with monkeypatch.context() as injected:
                    injected.setattr(identity.ScratchIdentitySession, 'load', interrupted)
                    return gate(*args)
        return gate(*args)
    monkeypatch.setattr(session, '_check_identity_anchor', anchor)
    with pytest.raises(signal_type) as error:
        execute(session, manifest)
    assert error.value is signal and signal.__traceback__ is not None
    if signal_type is SystemExit: assert signal.code == 187
    with pytest.raises(coordination.ScratchFilesystemReconciliationRequired) as repeated:
        execute(session, manifest)
    assert repeated.value.phase == state
    expected = {'PENDING': [], 'FS_APPLIED': ['PENDING'], 'FS_VERIFIED': ['PENDING', 'FS_APPLIED']}[state]
    assert states(session, manifest) == expected + ([state] if position == 'after' else [])


@pytest.mark.parametrize('invalid', ['production_root', 'manifest_id', 'action_id', 'index', 'adoption'])
def test_invalid_or_production_binding_never_captures_or_writes(case, monkeypatch, invalid):
    session, manifest, source, target = case
    action, index = manifest.payload.actions[0], 0
    if invalid == 'adoption':
        with pytest.raises(TypeError): coordination.ScratchActionSession.create(session.root.parent, root=source.parent)
        with pytest.raises(fs.RenameRejected): coordination.ScratchActionSession.create(Path('/mnt/nas-anime'))
        return
    if invalid == 'production_root': manifest = seal(manifest, library_root='/mnt/nas-anime')
    elif invalid == 'manifest_id': manifest = replace(manifest, manifest_id='0' * 64)
    elif invalid == 'action_id': action = replace(action, action_id='0' * 64)
    else: index = 1
    monkeypatch.setattr(identity.ScratchIdentitySession, 'capture', lambda *args, **kwargs: pytest.fail('invalid proposal captured'))
    with pytest.raises(coordination.ScratchCoordinationStopped) as error:
        session.execute(manifest, action, action_index=index)
    assert error.value.phase == 'VALIDATION'
    assert not evidence_path(session).exists() and not session.journal_path.exists()
    assert source.exists() and not target.exists()


def crash_child(parent, phase, pipe):
    """Deterministic process loss on isolated files; no real reboot/power fault."""
    session = coordination.ScratchActionSession.create(parent)
    manifest, source, target = prepare(session)
    pipe.send((session.root, session.evidence_path, session.journal_path, manifest))
    pipe.close()
    capture, append, rename = identity.ScratchIdentitySession.capture, journal.DurableJournal.append, fs._rename_noreplace
    def captured(owner, *args, **kwargs):
        result = capture(owner, *args, **kwargs)
        if phase == 'evidence': os._exit(83)
        return result
    def appended(writer, entry):
        append(writer, entry)
        if (phase, entry.state) in {('pending', 'PENDING'), ('applied', 'FS_APPLIED'), ('verified', 'FS_VERIFIED')}:
            os._exit(83)
    def renamed(*args):
        rename(*args)
        if phase == 'rename': os._exit(83)
    identity.ScratchIdentitySession.capture = captured
    journal.DurableJournal.append = appended
    fs._rename_noreplace = renamed
    execute(session, manifest)


@pytest.mark.parametrize('phase,expected,moved', [
    ('evidence', [], False), ('pending', ['PENDING'], False), ('rename', ['PENDING'], True),
    ('applied', ['PENDING', 'FS_APPLIED'], True), ('verified', ['PENDING', 'FS_APPLIED', 'FS_VERIFIED'], True),
])
def test_process_loss_preserves_identity_and_history_for_diagnostics_without_resume(tmp_path, phase, expected, moved):
    ctx = multiprocessing.get_context('fork')
    reader, writer = ctx.Pipe(duplex=False)
    process = ctx.Process(target=crash_child, args=(tmp_path, phase, writer))
    process.start()
    writer.close()
    try:
        assert reader.poll(10)
        root, path, journal_path, manifest = reader.recv()
        process.join(10)
        assert process.exitcode == 83
        raw = path.read_bytes()
        record = identity._decode_frame(raw)  # byte diagnostics only; not capture acknowledgement
        assert record.manifest_id == manifest.manifest_id and record.action == manifest.payload.actions[0]
        assert record.content.digest == '9b7080928611c26681c9b3ec3c9067065117368b09c737d72fda1219da79b90f'
        if expected:
            snapshot = journal.load_journal(journal_path, manifest)
            assert [entry.state for entry in snapshot.entries] == expected and snapshot.reconciliation_required
        else: assert not journal_path.exists()
        assert (root / 'old/source.mkv').exists() is not moved
        assert (root / 'new/target.mkv').exists() is moved
        with pytest.raises(TypeError): coordination.ScratchActionSession.create(tmp_path, root=root)
        with pytest.raises(TypeError): identity.ScratchIdentitySession(root=root, evidence_path=path)
        assert path.read_bytes() == raw and not hasattr(coordination.ScratchActionSession, 'resume')
    finally:
        reader.close()
        if process.is_alive():
            process.terminate()
            process.join(10)
