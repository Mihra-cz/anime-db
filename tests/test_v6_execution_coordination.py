"""E2B operations use exclusively caller-created pytest tmp_path assets.

Injected faults/crashes test protocol decisions, never production capabilities.
"""
from dataclasses import replace
import errno
import importlib
import importlib.util
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from app import target_execution_filesystem as fs
from app import target_execution_journal as journal
from app.target_execution_manifest import manifest_to_json
from test_v6_execution_filesystem import prepare, seal, changed_action


def api():
    assert importlib.util.find_spec('app.target_execution_coordination') is not None, \
        'scratch-only E2B coordination is missing'
    return importlib.import_module('app.target_execution_coordination')


@pytest.fixture
def case(tmp_path):
    with api().ScratchActionSession.create(tmp_path) as session:
        yield session, *prepare(session)


def states(session, manifest):
    snapshot = journal.load_journal(session.journal_path, manifest)
    assert snapshot.reconciliation_required is True
    return [entry.state for entry in snapshot.entries]


@pytest.mark.parametrize('action', ['MOVE', 'QUARANTINE'])
def test_one_action_verified_without_db_completion(tmp_path, action):
    with api().ScratchActionSession.create(tmp_path) as session:
        manifest, source, target = prepare(session, action=action)
        before, raw = source.stat(), manifest_to_json(manifest)
        result = session.execute(manifest, manifest.payload.actions[0], action_index=0)
        assert result.outcome == 'FS_VERIFIED'
        assert result.manifest_id == manifest.manifest_id
        assert result.action_id == manifest.payload.actions[0].action_id
        assert result.action_index == 0
        assert result.verification.device == before.st_dev
        assert result.verification.inode == before.st_ino
        assert not source.exists() and target.read_bytes() == b'original scratch video'
        assert states(session, manifest) == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED']
        assert manifest_to_json(manifest) == raw


def test_pending_durability_precedes_syscall_and_fresh_verification(case, monkeypatch):
    session, manifest, source, target = case
    events = []
    append, sync, rename = journal.DurableJournal.append, os.fsync, fs._rename_noreplace
    verify = fs.ScratchObservation.verify

    def record_sync(fd):
        sync(fd)
        if os.readlink(f'/proc/self/fd/{fd}') == str(session.journal_path):
            events.append('journal-fsync')

    def record_append(writer, entry):
        append(writer, entry)
        assert events[-1] == 'journal-fsync'
        events.append(entry.state)

    def record_rename(*args):
        assert events[-1] == 'PENDING'
        # Real on-disk canonical history is observed while the live lock is held.
        with session.journal_path.open() as stream:
            assert 'PENDING' in stream.read()
        events.append('rename')
        rename(*args)

    def record_verify(observation):
        assert events[-1] == 'FS_APPLIED'
        assert not source.exists() and target.exists()
        events.append('observe-target')
        return verify(observation)

    monkeypatch.setattr(os, 'fsync', record_sync)
    monkeypatch.setattr(journal.DurableJournal, 'append', record_append)
    monkeypatch.setattr(fs, '_rename_noreplace', record_rename)
    monkeypatch.setattr(fs.ScratchObservation, 'verify', record_verify)
    session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert [e for e in events if e != 'journal-fsync'] == [
        'PENDING', 'rename', 'FS_APPLIED', 'observe-target', 'FS_VERIFIED']


@pytest.mark.parametrize('state,expected,moved', [
    ('PENDING', [], False),
    ('FS_APPLIED', ['PENDING'], True),
    ('FS_VERIFIED', ['PENDING', 'FS_APPLIED'], True),
])
def test_append_failure_stops_without_retry_or_repair(case, monkeypatch, state, expected, moved):
    session, manifest, source, target = case
    append, calls = journal.DurableJournal.append, []
    rename = fs._rename_noreplace

    def fail(writer, entry):
        if entry.state == state:
            raise OSError(errno.EIO, 'injected journal failure')
        append(writer, entry)

    def count(*args):
        calls.append(1)
        return rename(*args)

    monkeypatch.setattr(journal.DurableJournal, 'append', fail)
    monkeypatch.setattr(fs, '_rename_noreplace', count)
    with pytest.raises(api().ScratchJournalReconciliationRequired):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert source.exists() is not moved and target.exists() is moved
    assert states(session, manifest) == expected
    raw = session.journal_path.read_bytes()
    with pytest.raises(api().ScratchCoordinationStopped):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert len(calls) == int(moved) and session.journal_path.read_bytes() == raw


def test_repeated_ambiguous_attempt_preserves_reconciliation_category(case, monkeypatch):
    session, manifest, source, target = case
    def fail(*args):
        raise OSError(errno.EIO, 'ambiguous syscall')
    monkeypatch.setattr(fs, '_rename_noreplace', fail)
    for _ in range(2):
        with pytest.raises(api().ScratchFilesystemReconciliationRequired):
            session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert states(session, manifest) == ['PENDING']


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_cleanup_error_cannot_mask_process_interruption(case, monkeypatch, interruption):
    session, manifest, source, target = case
    signal = interruption('interrupted syscall')
    close = journal.DurableJournal.close
    def interrupt(*args):
        raise signal
    def close_then_fail(writer):
        close(writer)
        raise OSError(errno.EIO, 'injected cleanup failure')
    monkeypatch.setattr(fs, '_rename_noreplace', interrupt)
    monkeypatch.setattr(journal.DurableJournal, 'close', close_then_fail)
    with pytest.raises(interruption) as error:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert error.value is signal
    assert any('cleanup' in note for note in signal.__notes__)
    assert states(session, manifest) == ['PENDING']


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_observation_descriptor_cleanup_preserves_interruption(case, monkeypatch, interruption):
    session, manifest, source, target = case
    signal = interruption('interrupted syscall')
    opened, open_fd, close_fd = set(), os.open, os.close
    def track_open(path, flags, *args, **kwargs):
        fd = open_fd(path, flags, *args, **kwargs)
        if path == source.name and 'dir_fd' in kwargs:
            opened.add(fd)
        return fd
    def fail_close(fd):
        close_fd(fd)
        if fd in opened:
            opened.remove(fd)
            raise OSError(errno.EIO, 'descriptor cleanup')
    def interrupt(*args):
        raise signal
    monkeypatch.setattr(os, 'open', track_open)
    monkeypatch.setattr(os, 'close', fail_close)
    monkeypatch.setattr(fs, '_rename_noreplace', interrupt)
    with pytest.raises(interruption) as error:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert error.value is signal
    assert any('cleanup' in note for note in signal.__notes__)
    assert states(session, manifest) == ['PENDING']


@pytest.mark.parametrize('primary', [None, 'journal', 'filesystem'])
def test_close_failure_is_typed_or_preserves_primary_reconciliation(case, monkeypatch, primary):
    session, manifest, source, target = case
    close = journal.DurableJournal.close
    def close_then_fail(writer):
        close(writer)
        raise OSError(errno.EIO, 'cleanup failure')
    monkeypatch.setattr(journal.DurableJournal, 'close', close_then_fail)
    if primary == 'journal':
        def fail_append(*args):
            raise OSError(errno.EIO, 'append failure')
        monkeypatch.setattr(journal.DurableJournal, 'append', fail_append)
    elif primary == 'filesystem':
        def fail_rename(*args):
            raise OSError(errno.EIO, 'syscall failure')
        monkeypatch.setattr(fs, '_rename_noreplace', fail_rename)
    expected = api().ScratchFilesystemReconciliationRequired if primary == 'filesystem' else api().ScratchJournalReconciliationRequired
    with pytest.raises(expected) as error:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    if primary is not None:
        assert any('cleanup' in note for note in error.value.__notes__)
    assert states(session, manifest) == ([] if primary == 'journal' else
        ['PENDING'] if primary == 'filesystem' else ['PENDING', 'FS_APPLIED', 'FS_VERIFIED'])


def test_outer_handled_exception_cannot_hide_close_failure(case, monkeypatch):
    session, manifest, source, target = case
    close = journal.DurableJournal.close
    def close_then_fail(writer):
        close(writer)
        raise OSError(errno.EIO, 'cleanup only')
    monkeypatch.setattr(journal.DurableJournal, 'close', close_then_fail)
    try:
        raise ValueError('unrelated caller error')
    except ValueError as outer:
        with pytest.raises(api().ScratchJournalReconciliationRequired):
            session.execute(manifest, manifest.payload.actions[0], action_index=0)
        assert not hasattr(outer, '__notes__')


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_cleanup_interruption_takes_priority_over_ordinary_close_error(case, monkeypatch, interruption):
    session, manifest, source, target = case
    signal = interruption('filesystem close interrupted')
    journal_close, fs_close = journal.DurableJournal.close, fs.ScratchRenameSession.close
    def fail_journal(writer):
        journal_close(writer)
        raise OSError(errno.EIO, 'journal close error')
    def interrupt_close(filesystem):
        fs_close(filesystem)
        raise signal
    monkeypatch.setattr(journal.DurableJournal, 'close', fail_journal)
    monkeypatch.setattr(fs.ScratchRenameSession, 'close', interrupt_close)
    with pytest.raises(interruption) as error:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert error.value is signal
    assert any('OSError' in note for note in signal.__notes__)
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED', 'FS_VERIFIED']


def test_outer_handled_exception_cannot_hide_descriptor_failure(case, monkeypatch):
    session, manifest, source, target = case
    opened, open_fd, close_fd = set(), os.open, os.close
    def track_open(path, flags, *args, **kwargs):
        fd = open_fd(path, flags, *args, **kwargs)
        if path == source.name and 'dir_fd' in kwargs:
            opened.add(fd)
        return fd
    def fail_close(fd):
        close_fd(fd)
        if fd in opened:
            opened.remove(fd)
            raise OSError(errno.EIO, 'descriptor cleanup only')
    monkeypatch.setattr(os, 'open', track_open)
    monkeypatch.setattr(os, 'close', fail_close)
    try:
        raise ValueError('unrelated caller error')
    except ValueError as outer:
        with pytest.raises(api().ScratchFilesystemReconciliationRequired):
            session.execute(manifest, manifest.payload.actions[0], action_index=0)
        assert not hasattr(outer, '__notes__')
    assert states(session, manifest) == ['PENDING']


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_header_interruption_with_cleanup_failure_keeps_original_signal(case, monkeypatch, interruption):
    session, manifest, source, target = case
    signal = interruption('journal header interrupted')
    close_fd = os.close
    def interrupt_write(fd, raw):
        os.write(fd, raw[:19])
        raise signal
    def fail_close(fd):
        is_journal = os.readlink(f'/proc/self/fd/{fd}') == str(session.journal_path)
        close_fd(fd)
        if is_journal:
            raise OSError(errno.EIO, 'header cleanup')
    monkeypatch.setattr(journal, '_write_all', interrupt_write)
    monkeypatch.setattr(os, 'close', fail_close)
    with pytest.raises(interruption) as error:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert error.value is signal and any('cleanup' in note for note in signal.__notes__)
    assert source.exists() and not target.exists()
    raw = session.journal_path.read_bytes()
    monkeypatch.setattr(os, 'close', close_fd)
    with pytest.raises(journal.ReconciliationRequired) as error:
        journal.load_journal(session.journal_path, manifest)
    assert error.value.entries == () and session.journal_path.read_bytes() == raw


@pytest.mark.parametrize('state', ['PENDING', 'FS_APPLIED', 'FS_VERIFIED'])
def test_journal_fsync_failure_keeps_unacknowledged_frame_diagnostic(case, monkeypatch, state):
    session, manifest, source, target = case
    append, sync = journal.DurableJournal.append, os.fsync

    def fail_append(writer, entry):
        if entry.state != state:
            return append(writer, entry)
        def fail_sync(fd):
            if fd == writer._fd:
                raise OSError(errno.EIO, 'journal fsync')
            return sync(fd)
        with monkeypatch.context() as patch:
            patch.setattr(os, 'fsync', fail_sync)
            return append(writer, entry)

    monkeypatch.setattr(journal.DurableJournal, 'append', fail_append)
    with pytest.raises(api().ScratchJournalReconciliationRequired):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    observed = states(session, manifest)
    assert observed[-1] == state  # bytes are not acknowledgement or resume authority
    assert source.exists() is (state == 'PENDING')
    assert target.exists() is (state != 'PENDING')


def test_failure_after_pending_before_syscall_is_stop(case, monkeypatch):
    session, manifest, source, target = case
    def reject(observation, *args):
        raise fs.RenameRejected('injected pre-syscall binding rejection')
    monkeypatch.setattr(fs.ScratchObservation, 'check_before', reject)
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('syscall started'))
    with pytest.raises(api().ScratchCoordinationStopped) as error:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert error.value.reconciliation_required is False
    assert states(session, manifest) == ['PENDING']
    assert source.exists() and not target.exists()


def test_pending_append_without_durable_acknowledged_entry_cannot_start_rename(case, monkeypatch):
    session, manifest, source, target = case
    monkeypatch.setattr(journal.DurableJournal, 'append', lambda *args: None)
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('unacknowledged PENDING renamed'))
    with pytest.raises(api().ScratchJournalReconciliationRequired):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert states(session, manifest) == []
    assert source.exists() and not target.exists()


@pytest.mark.parametrize('failure', ['header_write', 'header_fsync', 'foreign_envelope'])
def test_journal_creation_failure_preserves_header_and_never_renames(case, monkeypatch, failure):
    session, manifest, source, target = case
    create, sync = journal.DurableJournal.create, os.fsync
    if failure == 'header_write':
        def fail_write(fd, raw):
            os.write(fd, raw[:19])
            raise OSError(errno.EIO, 'partial header')
        monkeypatch.setattr(journal, '_write_all', fail_write)
    elif failure == 'header_fsync':
        def fail_sync(fd):
            if os.readlink(f'/proc/self/fd/{fd}') == str(session.journal_path):
                raise OSError(errno.EIO, 'header fsync')
            return sync(fd)
        monkeypatch.setattr(os, 'fsync', fail_sync)
    else:
        def foreign_create(cls, path, supplied):
            return create(path, replace(supplied, created_at='2026-10-09T00:00:00Z'))
        monkeypatch.setattr(journal.DurableJournal, 'create', classmethod(foreign_create))
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('invalid journal renamed'))
    with pytest.raises(api().ScratchJournalReconciliationRequired):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert session.journal_path.exists() and session.journal_path.read_bytes()
    assert source.exists() and not target.exists()


def test_rename_fsync_failure_does_not_record_fs_applied(case, monkeypatch):
    session, manifest, source, target = case
    sync = os.fsync
    def fail_sync(fd):
        if os.readlink(f'/proc/self/fd/{fd}') == str(target):
            raise OSError(errno.EIO, 'file durability failed after rename')
        return sync(fd)
    monkeypatch.setattr(os, 'fsync', fail_sync)
    with pytest.raises(api().ScratchFilesystemReconciliationRequired):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert not source.exists() and target.exists()
    assert states(session, manifest) == ['PENDING']


def test_verified_session_cannot_repeat_action(case):
    session, manifest, source, target = case
    session.execute(manifest, manifest.payload.actions[0], action_index=0)
    raw, identity = session.journal_path.read_bytes(), target.stat().st_ino
    with pytest.raises(api().ScratchCoordinationStopped):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert session.journal_path.read_bytes() == raw and target.stat().st_ino == identity


def test_coordination_never_connects_to_db(case, tmp_path, monkeypatch):
    session, manifest, source, target = case
    path = tmp_path / 'sentinel.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE sentinel (value TEXT)')
        connection.execute("INSERT INTO sentinel VALUES ('unchanged')")
    before = path.read_bytes()
    def reject_db(*args, **kwargs):
        pytest.fail('E2B connected to database')
    from sqlalchemy.engine import Engine
    monkeypatch.setattr(Engine, 'connect', reject_db)
    monkeypatch.setattr(sqlite3, 'connect', reject_db)
    session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert path.read_bytes() == before


@pytest.mark.parametrize('code,ambiguous', [(errno.EACCES, False), (errno.EIO, True), (errno.EINTR, True)])
def test_syscall_errors_do_not_apply_or_retry(case, monkeypatch, code, ambiguous):
    session, manifest, source, target = case
    calls = []
    def fail(*args):
        calls.append(1)
        raise OSError(code, 'injected syscall failure')
    monkeypatch.setattr(fs, '_rename_noreplace', fail)
    expected = api().ScratchFilesystemReconciliationRequired if ambiguous else api().ScratchCoordinationStopped
    with pytest.raises(expected) as error:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert error.value.reconciliation_required is ambiguous
    assert states(session, manifest) == ['PENDING']
    assert source.exists() and not target.exists()
    with pytest.raises(api().ScratchCoordinationStopped):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert calls == [1]


@pytest.mark.parametrize('mutation', ['target_inode', 'target_symlink', 'target_hardlink',
    'source_reappears', 'target_parent', 'source_parent_symlink', 'root', 'mtime', 'mount'])
def test_verification_reopens_and_checks_real_identity(case, monkeypatch, mutation):
    session, manifest, source, target = case
    append = journal.DurableJournal.append
    def mutate(writer, entry):
        append(writer, entry)
        if entry.state != 'FS_APPLIED':
            return
        if mutation in {'target_inode', 'target_symlink'}:
            info = target.stat()
            target.rename(target.with_suffix('.original'))
            if mutation == 'target_inode':
                target.write_bytes(b'original scratch video')
                target.chmod(0o600)
                os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            else:
                target.symlink_to(target.with_suffix('.original'))
        elif mutation == 'target_hardlink':
            os.link(target, target.with_suffix('.alias'))
        elif mutation == 'source_reappears':
            source.write_bytes(b'unexpected source')
        elif mutation == 'target_parent':
            target.parent.rename(session.root / 'displaced-parent')
            target.parent.mkdir(mode=0o700)
        elif mutation == 'source_parent_symlink':
            source.parent.rename(session.root / 'displaced-parent')
            source.parent.symlink_to(session.root / 'displaced-parent', target_is_directory=True)
        elif mutation == 'root':
            session.root.rename(session.root.with_suffix('.displaced'))
            session.root.mkdir(mode=0o700)
        elif mutation == 'mtime':
            info = target.stat()
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        else:
            read = fs._read_mounts
            monkeypatch.setattr(fs, '_read_mounts', lambda: read()[:-1])
    monkeypatch.setattr(journal.DurableJournal, 'append', mutate)
    with pytest.raises(api().ScratchFilesystemReconciliationRequired):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED']


def test_pre_observation_identity_constrains_actual_rename(case, monkeypatch):
    session, manifest, source, target = case
    append = journal.DurableJournal.append
    def replace_source(writer, entry):
        append(writer, entry)
        if entry.state == 'PENDING':
            info = source.stat()
            source.rename(source.with_suffix('.original'))
            source.write_bytes(b'original scratch video')
            source.chmod(0o600)
            os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    monkeypatch.setattr(journal.DurableJournal, 'append', replace_source)
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('foreign inode renamed'))
    with pytest.raises(api().ScratchCoordinationStopped):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert source.exists() and not target.exists()
    assert states(session, manifest) == ['PENDING']


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('phase,category', [('pending', 'Journal'), ('syscall', 'Filesystem'),
    ('after_syscall', 'Filesystem'), ('applied_append', 'Journal'), ('verification', 'Filesystem'),
    ('verified_append', 'Journal')])
def test_process_interruptions_preserve_class_and_stop(case, monkeypatch, interruption, phase, category):
    session, manifest, source, target = case
    signal = interruption('injected process interruption')
    append, rename = journal.DurableJournal.append, fs._rename_noreplace
    appended = {'pending': 'PENDING', 'applied_append': 'FS_APPLIED', 'verified_append': 'FS_VERIFIED'}
    def interrupted(*args):
        if phase == 'after_syscall':
            rename(*args)
        raise signal
    def interrupted_append(writer, entry):
        if entry.state == appended[phase]:
            raise signal
        return append(writer, entry)
    if phase in appended:
        monkeypatch.setattr(journal.DurableJournal, 'append', interrupted_append)
    elif phase == 'verification':
        monkeypatch.setattr(fs.ScratchObservation, 'verify', interrupted)
    else:
        monkeypatch.setattr(fs, '_rename_noreplace', interrupted)
    with pytest.raises(interruption) as error:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert error.value is signal
    assert any('STOP' in note for note in error.value.__notes__)
    # The spent session keeps the interrupted phase's reconciliation category.
    with pytest.raises(getattr(api(), f'Scratch{category}ReconciliationRequired')):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert 'FAILED' not in states(session, manifest)


@pytest.mark.parametrize('invalid', ['manifest_id', 'action_id', 'payload', 'index', 'bool_index',
    'foreign_root', 'mount', 'keep', 'multiple', 'review', 'dependency', 'diagnostics', 'traversal'])
def test_invalid_identity_rejected_before_journal_or_rename(case, monkeypatch, invalid):
    session, manifest, source, target = case
    action, index = manifest.payload.actions[0], 0
    if invalid == 'manifest_id':
        manifest = replace(manifest, manifest_id='0' * 64)
    elif invalid == 'action_id':
        action = replace(action, action_id='0' * 64)
    elif invalid == 'payload':
        action = replace(action, expected_size=action.expected_size + 1)
    elif invalid in {'index', 'bool_index'}:
        index = 1 if invalid == 'index' else False
    elif invalid == 'foreign_root':
        manifest = seal(manifest, library_root='/existing/production/library')
    elif invalid == 'mount':
        manifest = seal(manifest, mount_identity='wrong')
    elif invalid == 'keep':
        action = changed_action(manifest, action='KEEP', target=action.source, expected_target_absent=False)
        manifest = seal(manifest, (action,))
    elif invalid == 'review':
        action = changed_action(manifest, criticality='REVIEW', failure_policy='REVIEW')
        manifest = seal(manifest, (action,))
    elif invalid == 'dependency':
        action = replace(action, depends_on=(action.action_id,))
        manifest = seal(manifest, (action,))
    elif invalid == 'diagnostics':
        manifest = seal(manifest, generation_diagnostics=('unresolved',))
    elif invalid == 'traversal':
        action = changed_action(manifest, target='../escaped.mkv')
        manifest = seal(manifest, (action,))
    else:
        second = changed_action(manifest, object_id=action.object_id + 1, source='old/second.mkv', target='new/second.mkv')
        manifest = seal(manifest, (action, second))
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('invalid identity renamed'))
    with pytest.raises(api().ScratchCoordinationStopped) as error:
        session.execute(manifest, action, action_index=index)
    assert error.value.reconciliation_required is False
    assert not session.journal_path.exists()
    assert source.exists() and not target.exists()
    assert not (session.root.parent / 'escaped.mkv').exists()


def test_existing_target_preserved_and_no_failed_assertions(case):
    session, manifest, source, target = case
    target.write_bytes(b'preserve target')
    with pytest.raises(api().ScratchCoordinationStopped):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert source.read_bytes() == b'original scratch video'
    assert target.read_bytes() == b'preserve target'
    assert not session.journal_path.exists()


def test_existing_journal_is_not_opened_for_execution(case):
    session, manifest, source, target = case
    session.journal_path.write_bytes(b'previous interrupted history\n')
    before = session.journal_path.read_bytes()
    with pytest.raises(api().ScratchJournalReconciliationRequired):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert session.journal_path.read_bytes() == before
    assert source.exists() and not target.exists()


def test_corrupt_live_journal_keeps_diagnostic_prefix_and_never_renames(case, monkeypatch):
    session, manifest, source, target = case
    append = journal.DurableJournal.append
    def corrupt(writer, entry):
        append(writer, entry)
        with session.journal_path.open('ab') as stream:
            stream.write(b'{"incomplete":')
    monkeypatch.setattr(journal.DurableJournal, 'append', corrupt)
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('corrupt journal renamed'))
    with pytest.raises(api().ScratchJournalReconciliationRequired):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    raw = session.journal_path.read_bytes()
    with pytest.raises(journal.ReconciliationRequired) as error:
        journal.load_journal(session.journal_path, manifest)
    assert [e.state for e in error.value.entries] == ['PENDING']
    assert session.journal_path.read_bytes() == raw and source.exists() and not target.exists()


def test_valid_unacknowledged_frame_is_not_live_history(case, monkeypatch):
    """A canonical, correctly chained frame from outside the writer still requires STOP."""
    session, manifest, source, target = case
    append = journal.DurableJournal.append
    def forge(writer, entry):
        append(writer, entry)
        forged = replace(entry, state='FS_APPLIED')
        raw, _ = journal._frame({'sequence': 2, 'previous_sha256': writer._digest,
            'entry': journal._strict_json_load(journal.journal_to_json(forged))})
        with session.journal_path.open('ab') as stream:
            stream.write(raw)
    monkeypatch.setattr(journal.DurableJournal, 'append', forge)
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('forged history renamed'))
    with pytest.raises(api().ScratchJournalReconciliationRequired):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert states(session, manifest) == ['PENDING', 'FS_APPLIED']  # diagnostics only
    assert source.exists() and not target.exists()


def test_no_existing_root_or_writer_adoption_api(tmp_path):
    coord = api()
    with pytest.raises(TypeError):
        coord.ScratchActionSession()
    with pytest.raises(TypeError):
        coord.ScratchActionSession.create(tmp_path, root=tmp_path)
    with pytest.raises(fs.RenameRejected):
        coord.ScratchActionSession.create(Path('/existing/production/library'))
    assert not hasattr(coord.ScratchActionSession, 'resume')
    assert not hasattr(coord.ScratchActionSession, 'open')


def crash_child(parent, phase, pipe):
    """Actual fork exit at deterministic boundaries; no recovery writer exists."""
    session = api().ScratchActionSession.create(parent)
    manifest, source, target = prepare(session)
    pipe.send((session.root, session.journal_path, manifest))
    pipe.close()
    create = journal.DurableJournal.create
    append, rename, verify = journal.DurableJournal.append, fs._rename_noreplace, fs.ScratchObservation.verify
    def exit_after_create(cls, path, supplied):
        result = create(path, supplied)
        if phase == 'header':
            os._exit(73)
        return result
    def exit_after_append(writer, entry):
        if (phase, entry.state) == ('durable', 'FS_APPLIED'):
            os._exit(73)  # rename and its fsync barriers done; FS_APPLIED never written
        append(writer, entry)
        if (phase, entry.state) in {('pending', 'PENDING'), ('applied', 'FS_APPLIED'), ('verified', 'FS_VERIFIED')}:
            os._exit(73)
    def exit_after_rename(*args):
        rename(*args)
        if phase == 'rename':
            os._exit(73)
    def exit_after_verify(observation):
        result = verify(observation)
        if phase == 'observed':
            os._exit(73)
        return result
    journal.DurableJournal.append = exit_after_append
    journal.DurableJournal.create = classmethod(exit_after_create)
    fs._rename_noreplace = exit_after_rename
    fs.ScratchObservation.verify = exit_after_verify
    session.execute(manifest, manifest.payload.actions[0], action_index=0)


@pytest.mark.parametrize('phase,expected,moved', [
    ('header', [], False),
    ('pending', ['PENDING'], False), ('rename', ['PENDING'], True),
    ('durable', ['PENDING'], True),
    ('applied', ['PENDING', 'FS_APPLIED'], True),
    ('observed', ['PENDING', 'FS_APPLIED'], True),
    ('verified', ['PENDING', 'FS_APPLIED', 'FS_VERIFIED'], True),
])
def test_process_crash_allows_diagnostics_only(tmp_path, phase, expected, moved):
    api()  # fail cleanly before spawning if implementation is missing
    ctx = multiprocessing.get_context('fork')
    reader, writer = ctx.Pipe(duplex=False)
    process = ctx.Process(target=crash_child, args=(tmp_path, phase, writer))
    process.start()
    writer.close()
    try:
        assert reader.poll(10), 'child did not prepare scratch fixture'
        root, path, manifest = reader.recv()
        process.join(10)
        assert process.exitcode == 73
        snapshot = journal.load_journal(path, manifest)
        assert snapshot.reconciliation_required is True
        assert [e.state for e in snapshot.entries] == expected
        assert (root / 'old/source.mkv').exists() is not moved
        assert (root / 'new/target.mkv').exists() is moved
        raw = path.read_bytes()
        with pytest.raises(FileExistsError):
            journal.DurableJournal.create(path, manifest)
        assert path.read_bytes() == raw
    finally:
        reader.close()
        if process.is_alive():
            process.terminate()
            process.join(10)
