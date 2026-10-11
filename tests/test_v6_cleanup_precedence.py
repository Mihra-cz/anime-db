"""Cleanup faults use only files/session roots created in pytest tmp_path."""
from contextlib import ExitStack
import errno
import io
import os

import pytest

from app import target_execution_coordination as coordination
from app import target_execution_filesystem as filesystem
from app import target_execution_journal as journal
from test_v6_execution_filesystem import prepare
from test_v6_execution_journal import scratch_manifest, entry


CASES = [
    ('stop', ('io',)),
    ('stop', ('keyboard',)),
    ('stop', ('exit',)),
    ('keyboard', ('io',)),
    ('exit', ('io',)),
    ('keyboard', ('exit',)),
    ('exit', ('keyboard',)),
    ('exit', ('exit',)),
    (None, ('keyboard',)),
    (None, ('exit',)),
    ('stop', ('io', 'exit')),
    ('stop', ('keyboard', 'io')),
    ('stop', ('io', 'io')),
    ('keyboard', ('io', 'exit')),
    (None, ('io', 'keyboard')),
    (None, ('exit', 'keyboard')),
    (None, ('io',)),
    (None, ('io', 'io')),
]


def failure(kind, label, code=71):
    if kind is None:
        return None
    if kind == 'keyboard':
        return KeyboardInterrupt(label)
    if kind == 'exit':
        return SystemExit(code)
    return OSError(errno.EIO, label)


def interrupt(exc):
    return isinstance(exc, (KeyboardInterrupt, SystemExit))


def selected(primary, cleanups):
    if interrupt(primary):
        return primary
    return next((exc for exc in cleanups if interrupt(exc)), primary or cleanups[0])


def evidence(exc):
    """Traverse explicit cause/context, preserving earlier traceback evidence."""
    result, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        result.append(exc)
        exc = exc.__cause__ or exc.__context__
    return result


def assert_diagnostics(actual, primary, cleanups):
    chain = evidence(actual)
    diagnostic = '\n'.join(str(exc) + '\n' + '\n'.join(getattr(exc, '__notes__', ())) for exc in chain)
    if primary is not None:
        assert primary in chain
        assert primary.__traceback__ is not None
    for exc in cleanups:
        assert str(exc) in diagnostic
        assert exc.__traceback__ is not None
        traceback = exc.__traceback__
        names = []
        while traceback is not None:
            names.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        assert any(name in {'fail_close', 'fail_writer_close', 'fail_fs_close', 'close'} for name in names)
    expected = selected(primary, cleanups)
    if isinstance(expected, SystemExit):
        assert actual.code == expected.code
    frames = []
    traceback = actual.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert frames  # the injected raise remains available through the chain


@pytest.mark.parametrize('primary_kind,cleanup_kinds', CASES)
def test_e1_writer_cleanup_precedence_and_no_append_retry(tmp_path, monkeypatch, primary_kind, cleanup_kinds):
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    writer = journal.DurableJournal.create(path, manifest)
    primary = failure(primary_kind, 'primary E1 STOP')
    cleanups = [failure(kind, f'E1 cleanup {i}', 80 + i) for i, kind in enumerate(cleanup_kinds)]
    fds, closes, writes = (writer._fd, writer._parent_fd), [], []
    close, write = os.close, journal._write_all
    def fail_close(fd):
        close(fd)
        if fd in fds:
            closes.append(fd)
            index = fds.index(fd)
            if index < len(cleanups):
                raise cleanups[index]
    def fail_write(fd, raw):
        writes.append(fd)
        if primary is not None:
            raise primary
        write(fd, raw)
    monkeypatch.setattr(os, 'close', fail_close)
    monkeypatch.setattr(journal, '_write_all', fail_write)
    expected = selected(primary, cleanups)
    with pytest.raises(BaseException) as error:
        with writer:
            writer.append(entry(manifest))
    assert error.value is expected
    assert_diagnostics(error.value, primary, cleanups)
    assert closes == list(fds) and len(writes) == 1
    assert writer._closed and (writer._poisoned or primary is None)
    raw = path.read_bytes()
    writer.close()
    with pytest.raises(journal.JournalError):
        writer.append(entry(manifest))
    assert closes == list(fds) and len(writes) == 1 and path.read_bytes() == raw
    monkeypatch.setattr(os, 'close', close)
    snapshot = journal.load_journal(path, manifest)
    assert [item.state for item in snapshot.entries] == ([] if primary else ['PENDING'])


@pytest.mark.parametrize('primary_kind,cleanup_kinds', CASES)
def test_e2a_held_evidence_precedence_keeps_all_cleanup(tmp_path, monkeypatch, primary_kind, cleanup_kinds):
    fds = []
    for index in range(2):
        path = tmp_path / f'evidence-{index}'
        path.write_bytes(b'isolated evidence')
        fds.append(os.open(path, os.O_RDONLY))
    primary = filesystem.RenameRejected('primary E2A STOP') if primary_kind == 'stop' else failure(primary_kind, 'primary E2A interruption')
    cleanups = [failure(kind, f'E2A cleanup {i}', 80 + i) for i, kind in enumerate(cleanup_kinds)]
    close, closes = os.close, []
    def fail_close(fd):
        close(fd)
        closes.append(fd)
        index = fds.index(fd)
        if index < len(cleanups):
            raise cleanups[index]
    monkeypatch.setattr(os, 'close', fail_close)
    expected = selected(primary, cleanups)
    with pytest.raises(BaseException) as error:
        with ExitStack() as stack:
            for fd in reversed(fds):
                stack.enter_context(filesystem._held_evidence_fd(fd))
            if primary is not None:
                raise primary
    assert error.value is expected
    assert_diagnostics(error.value, primary, cleanups)
    assert closes == fds


@pytest.mark.parametrize('primary_kind,cleanup_kinds', CASES)
def test_e2b_finish_precedence_preserves_history_and_spent_session(tmp_path, monkeypatch, primary_kind, cleanup_kinds):
    session = coordination.ScratchActionSession.create(tmp_path)
    manifest, source, target = prepare(session)
    primary_fault = failure(primary_kind, 'primary E2B append failure')
    cleanups = [failure(kind, f'E2B cleanup {i}', 80 + i) for i, kind in enumerate(cleanup_kinds)]
    observed, closes, appends, renames = [], [], [], []
    finish, write = session._finish, journal._write_all
    append, rename = journal.DurableJournal.append, filesystem._rename_noreplace
    writer_close, fs_close = journal.DurableJournal.close, filesystem.ScratchRenameSession.close
    def record_finish(writer, primary):
        observed.append(primary)
        return finish(writer, primary)
    def fail_write(fd, raw):
        if primary_fault is not None and b'"entry":' in raw:
            raise primary_fault
        write(fd, raw)
    def record_append(writer, item):
        appends.append(item.state)
        return append(writer, item)
    def record_rename(*args):
        renames.append(1)
        return rename(*args)
    def fail_writer_close(writer):
        writer_close(writer)
        closes.append('journal')
        raise cleanups[0]
    def fail_fs_close(fs):
        fs_close(fs)
        closes.append('filesystem')
        if len(cleanups) > 1:
            raise cleanups[1]
    monkeypatch.setattr(session, '_finish', record_finish)
    monkeypatch.setattr(journal, '_write_all', fail_write)
    monkeypatch.setattr(journal.DurableJournal, 'append', record_append)
    monkeypatch.setattr(filesystem, '_rename_noreplace', record_rename)
    monkeypatch.setattr(journal.DurableJournal, 'close', fail_writer_close)
    monkeypatch.setattr(filesystem.ScratchRenameSession, 'close', fail_fs_close)
    with pytest.raises(BaseException) as error:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    primary = observed[0]
    expected = selected(primary, cleanups)
    if primary is not None or interrupt(expected):
        assert error.value is expected
    else:
        assert isinstance(error.value, coordination.ScratchJournalReconciliationRequired)
        assert error.value.__cause__ is expected
    assert_diagnostics(error.value, primary, cleanups)
    assert closes == ['journal', 'filesystem']
    assert appends == (['PENDING'] if primary_fault else ['PENDING', 'FS_APPLIED', 'FS_VERIFIED'])
    assert len(renames) == (0 if primary_fault else 1)
    assert session._spent and session._closed and session._filesystem._closed
    raw = session.journal_path.read_bytes()
    with pytest.raises(coordination.ScratchCoordinationStopped):
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    session.close()
    assert closes == ['journal', 'filesystem'] and session.journal_path.read_bytes() == raw
    snapshot = journal.load_journal(session.journal_path, manifest)
    assert [item.state for item in snapshot.entries] == ([] if primary_fault else ['PENDING', 'FS_APPLIED', 'FS_VERIFIED'])
    assert source.exists() is bool(primary_fault) and target.exists() is (primary_fault is None)


@pytest.mark.parametrize('cleanup_kind', ['keyboard', 'exit'])
def test_e1_creation_cleanup_interrupt_does_not_skip_parent_close(tmp_path, monkeypatch, cleanup_kind):
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    primary = journal.ReconciliationRequired('header write STOP')
    signal = failure(cleanup_kind, 'header cleanup', 93)
    close, watched, closes = os.close, set(), []
    def fail_write(fd, raw):
        watched.add(fd)
        os.write(fd, raw[:17])
        raise primary
    def fail_close(fd):
        name = os.readlink(f'/proc/self/fd/{fd}')
        close(fd)
        if fd in watched or name == str(tmp_path):
            closes.append(name)
        if fd in watched:
            raise signal
    monkeypatch.setattr(journal, '_write_all', fail_write)
    monkeypatch.setattr(os, 'close', fail_close)
    with pytest.raises(BaseException) as error:
        journal.DurableJournal.create(path, manifest)
    assert error.value is signal and primary in evidence(signal)
    assert closes[-2:] == [str(path), str(tmp_path)]
    assert closes.count(str(path)) == 1
    if isinstance(signal, SystemExit):
        assert signal.code == 93
    assert path.read_bytes()


@pytest.mark.parametrize('layer', ['e2a', 'e2b'])
@pytest.mark.parametrize('primary_kind,cleanup_kind', [
    ('stop', 'io'), ('stop', 'keyboard'), ('stop', 'exit'),
    ('keyboard', 'io'), ('exit', 'io'), ('keyboard', 'exit'), ('exit', 'exit'),
    (None, 'keyboard'), (None, 'exit'),
])
def test_scratch_context_exit_preserves_precedence(tmp_path, monkeypatch, layer, primary_kind, cleanup_kind):
    cls = filesystem.ScratchRenameSession if layer == 'e2a' else coordination.ScratchActionSession
    session = cls.create(tmp_path)
    primary = failure(primary_kind, 'context body STOP')
    cleanup = failure(cleanup_kind, 'context cleanup', 87)
    close, calls = cls.close, []
    def fail_close(current):
        close(current)
        calls.append(1)
        raise cleanup
    monkeypatch.setattr(cls, 'close', fail_close)
    expected = selected(primary, [cleanup])
    with pytest.raises(BaseException) as error:
        with session:
            if primary is not None:
                raise primary
    assert error.value is expected
    assert_diagnostics(error.value, primary, [cleanup])
    assert session._closed and calls == [1]
    monkeypatch.setattr(cls, 'close', close)
    session.close()
    assert calls == [1]


@pytest.mark.parametrize('primary_kind', ['stop', 'keyboard', 'exit'])
@pytest.mark.parametrize('cleanup_kind', ['io', 'keyboard', 'exit'])
def test_e2a_syscall_failure_cleanup_keeps_session_poisoned(tmp_path, monkeypatch, primary_kind, cleanup_kind):
    with filesystem.ScratchRenameSession.create(tmp_path) as session:
        manifest, source, target = prepare(session)
        primary = failure(primary_kind, 'syscall failure')
        cleanup = failure(cleanup_kind, 'source descriptor cleanup', 89)
        close, calls, source_closes = os.close, [], []
        def fail_syscall(*args):
            calls.append(1)
            raise primary
        def fail_close(fd):
            is_source = os.readlink(f'/proc/self/fd/{fd}') == str(source)
            close(fd)
            if is_source:
                source_closes.append(fd)
                raise cleanup
        monkeypatch.setattr(filesystem, '_rename_noreplace', fail_syscall)
        monkeypatch.setattr(os, 'close', fail_close)
        with pytest.raises(BaseException) as error:
            session.rename(manifest, manifest.payload.actions[0])
        expected = selected(primary, [cleanup])
        if interrupt(expected):
            assert error.value is expected
        else:
            assert isinstance(error.value, filesystem.RenameReconciliationRequired)
            assert error.value.__cause__ is primary
        assert_diagnostics(error.value, primary, [cleanup])
        assert session._poisoned and len(source_closes) == 1
        with pytest.raises(filesystem.RenameReconciliationRequired):
            session.rename(manifest, manifest.payload.actions[0])
        assert calls == [1] and source.exists() and not target.exists()


@pytest.mark.parametrize('primary_kind,cleanup_kind', [
    ('stop', 'io'), ('stop', 'keyboard'), ('stop', 'exit'),
    ('keyboard', 'io'), ('exit', 'io'), ('exit', 'exit'),
    (None, 'keyboard'), (None, 'exit'),
])
def test_e1_history_stream_cleanup_preserves_reader_failure(tmp_path, monkeypatch, primary_kind, cleanup_kind):
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest):
        pass
    raw = path.read_bytes()
    primary = failure(primary_kind, 'history read failure')
    cleanup = failure(cleanup_kind, 'stream cleanup', 95)
    open_stream, closes = io.open, []
    class FaultStream:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def readline(self, *args):
            if primary is not None:
                raise primary
            return self.stream.readline(*args)
        def close(self):
            self.stream.close()
            closes.append(1)
            raise cleanup
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
    expected = selected(primary, [cleanup])
    def open_reader(file, *args, **kwargs):
        stream = open_stream(file, *args, **kwargs)
        # Inject only the held-fd reader; local path/mount checks stay real.
        return FaultStream(stream) if isinstance(file, int) or kwargs.get('opener') else stream
    with monkeypatch.context() as injected:
        injected.setattr(io, 'open', open_reader)
        with pytest.raises(BaseException) as error:
            journal.load_journal(path, manifest)
    assert error.value is expected
    assert_diagnostics(error.value, primary, [cleanup])
    assert closes == [1] and path.read_bytes() == raw


@pytest.mark.parametrize('cleanup_kind', ['io', 'keyboard', 'exit'])
def test_e2a_failed_allocation_closes_both_descriptors_once(tmp_path, monkeypatch, cleanup_kind):
    primary = filesystem.RenameRejected('allocation mount STOP')
    cleanup = failure(cleanup_kind, 'allocated root cleanup', 97)
    check, close, closes = filesystem._check_mounts, os.close, []
    def reject(root, fd, mounts):
        if os.readlink(f'/proc/self/fd/{fd}') != str(tmp_path):
            raise primary
        return check(root, fd, mounts)
    def fail_close(fd):
        name = os.readlink(f'/proc/self/fd/{fd}')
        close(fd)
        if name == str(tmp_path) or name.startswith(str(tmp_path / 'v6-e2a-')):
            closes.append(name)
        if name.startswith(str(tmp_path / 'v6-e2a-')):
            raise cleanup
    monkeypatch.setattr(filesystem, '_check_mounts', reject)
    monkeypatch.setattr(os, 'close', fail_close)
    expected = selected(primary, [cleanup])
    with pytest.raises(BaseException) as error:
        filesystem.ScratchRenameSession.create(tmp_path)
    assert error.value is expected
    assert_diagnostics(error.value, primary, [cleanup])
    assert len(closes) == 2 and closes[-1] == str(tmp_path)


@pytest.mark.parametrize('primary_kind', ['stop', 'keyboard', 'exit'])
def test_e1_loader_completes_cleanup_and_preserves_prefix(tmp_path, monkeypatch, primary_kind):
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest):
        pass
    primary = journal.ReconciliationRequired('history STOP', (entry(manifest),)) if primary_kind == 'stop' else failure(primary_kind, 'read interruption')
    signal, ordinary = SystemExit(99), OSError(errno.EIO, 'loader parent cleanup')
    close, closes, parent_fds = os.close, [], []
    open_parent = journal._open_parent
    def record_parent(*args):
        fd = open_parent(*args)
        parent_fds.append(fd)
        return fd
    def fail_read(*args):
        raise primary
    def fail_close(fd):
        name = os.readlink(f'/proc/self/fd/{fd}')
        close(fd)
        if name == str(path) or fd in parent_fds:
            closes.append(name)
            raise signal if name == str(path) else ordinary
    monkeypatch.setattr(journal, '_read_history', fail_read)
    monkeypatch.setattr(journal, '_open_parent', record_parent)
    monkeypatch.setattr(os, 'close', fail_close)
    raw = path.read_bytes()
    with pytest.raises(BaseException) as error:
        journal.load_journal(path, manifest)
    assert error.value is selected(primary, [signal, ordinary])
    assert_diagnostics(error.value, primary, [signal, ordinary])
    assert closes == [str(path), str(tmp_path)] and path.read_bytes() == raw
    if primary_kind == 'stop':
        assert primary.entries == (entry(manifest),) and primary in evidence(error.value)


@pytest.mark.parametrize('layer', ['e1_open', 'e1_check', 'e2a_open'])
@pytest.mark.parametrize('cleanup_kind', ['io', 'keyboard', 'exit'])
def test_directory_walk_does_not_retry_uncertain_close(tmp_path, monkeypatch, layer, cleanup_kind):
    child = tmp_path / 'child'
    child.mkdir(mode=0o700)
    manifest = scratch_manifest(tmp_path)
    cleanup = failure(cleanup_kind, 'ancestor close', 101)
    open_fd, close_fd = os.open, os.close
    parent_fd = open_fd(child, os.O_RDONLY | os.O_DIRECTORY) if layer == 'e1_check' else None
    opened, closed = [], []
    def track_open(*args, **kwargs):
        fd = open_fd(*args, **kwargs)
        opened.append(fd)
        return fd
    def fail_close(fd):
        try:
            name = os.readlink(f'/proc/self/fd/{fd}')
        except FileNotFoundError:
            name = 'already closed'
        closed.append(name)
        close_fd(fd)
        if name == str(tmp_path):
            raise cleanup
    monkeypatch.setattr(os, 'open', track_open)
    monkeypatch.setattr(os, 'close', fail_close)
    try:
        with pytest.raises(BaseException) as error:
            if layer == 'e2a_open':
                filesystem._open_absolute(child)
            elif layer == 'e1_check':
                journal._check_parent(child / 'journal.jsonl', parent_fd)
            else:
                journal._open_parent(child / 'journal.jsonl', manifest, (), ())
        assert error.value is cleanup
        assert closed[-2:] == [str(tmp_path), str(child)]
    finally:
        # The baseline leaks the newly opened child. Close only proven live fds
        # after assertions, never repeat a close whose result is uncertain.
        for fd in set(opened):
            try:
                os.fstat(fd)
            except OSError:
                continue
            close_fd(fd)
        if parent_fd is not None:
            close_fd(parent_fd)


@pytest.mark.parametrize('parent_kind,root_kind', [
    ('io', None), ('keyboard', None), ('exit', None),
    ('io', 'keyboard'), ('exit', 'exit'), ('keyboard', 'io'),
])
def test_e2a_unreturned_session_releases_root_after_parent_cleanup_fault(tmp_path, monkeypatch, parent_kind, root_kind):
    parent_error = failure(parent_kind, 'parent allocation cleanup', 103)
    root_error = failure(root_kind, 'unreturned root cleanup', 107)
    close, calls = os.close, []
    def fail_close(fd):
        name = os.readlink(f'/proc/self/fd/{fd}')
        close(fd)
        if name == str(tmp_path):
            calls.append('parent')
            raise parent_error
        if name.startswith(str(tmp_path / 'v6-e2a-')):
            calls.append('root')
            if root_error is not None:
                raise root_error
    monkeypatch.setattr(os, 'close', fail_close)
    with pytest.raises(BaseException) as error:
        filesystem.ScratchRenameSession.create(tmp_path)
    expected = selected(parent_error, [root_error]) if root_error is not None else parent_error
    assert error.value is expected and calls == ['parent', 'root']
    if root_error is not None:
        assert_diagnostics(error.value, parent_error, [root_error])
    if isinstance(expected, SystemExit):
        assert error.value.code == 103


@pytest.mark.parametrize('operation', ['rename', 'observe'])
@pytest.mark.parametrize('cleanup_kind', ['keyboard', 'exit'])
def test_e2a_cleanup_interruption_before_syscall_blocks_session(tmp_path, monkeypatch, operation, cleanup_kind):
    with filesystem.ScratchRenameSession.create(tmp_path) as session:
        manifest, source, target = prepare(session)
        signal = failure(cleanup_kind, 'pre-syscall cleanup', 109)
        close = os.close
        def fail_close(fd):
            is_source = os.readlink(f'/proc/self/fd/{fd}') == str(source)
            close(fd)
            if is_source:
                raise signal
        monkeypatch.setattr(os, 'close', fail_close)
        monkeypatch.setattr(filesystem, '_rename_noreplace', lambda *args: pytest.fail('syscall started'))
        with pytest.raises(BaseException) as error:
            if operation == 'rename':
                source.write_bytes(b'changed scratch size')
                session.rename(manifest, manifest.payload.actions[0])
            else:
                with session.observe(manifest, manifest.payload.actions[0]):
                    raise filesystem.RenameRejected('observation body STOP')
        assert error.value is signal and session._poisoned
        with pytest.raises(filesystem.RenameReconciliationRequired):
            session.rename(manifest, manifest.payload.actions[0])
        assert source.exists() and not target.exists()


@pytest.mark.parametrize('cleanup_kind', ['keyboard', 'exit'])
def test_e2a_partial_session_cleanup_never_closes_root_twice(tmp_path, monkeypatch, cleanup_kind):
    primary = filesystem.RenameRejected('partially initialized session STOP')
    signal = failure(cleanup_kind, 'partial session cleanup', 117)
    close, closes, root_fds = os.close, [], set()
    def fail_identity(info):
        raise primary
    def fail_close(fd):
        if fd in root_fds:
            closes.append('repeated root close')
        else:
            name = os.readlink(f'/proc/self/fd/{fd}')
            if name.startswith(str(tmp_path / 'v6-e2a-')):
                root_fds.add(fd)
                closes.append('root')
            elif name == str(tmp_path):
                closes.append('parent')
        close(fd)
        if fd in root_fds:
            raise signal
    monkeypatch.setattr(filesystem, '_identity', fail_identity)
    monkeypatch.setattr(os, 'close', fail_close)
    with pytest.raises(BaseException) as error:
        filesystem.ScratchRenameSession.create(tmp_path)
    assert error.value is signal and primary in evidence(signal)
    assert closes == ['root', 'parent']
    if isinstance(signal, SystemExit):
        assert signal.code == 117


@pytest.mark.parametrize('resource', ['journal', 'filesystem'])
@pytest.mark.parametrize('cleanup_kind', ['keyboard', 'exit'])
@pytest.mark.parametrize('code,stop', [(errno.EIO, 'ScratchFilesystemReconciliationRequired'),
    (errno.EACCES, 'ScratchCoordinationStopped')])
def test_e2b_cleanup_interruption_keeps_primary_stop_classification(tmp_path, monkeypatch, resource,
                                                                    cleanup_kind, code, stop):
    session = coordination.ScratchActionSession.create(tmp_path)
    manifest, source, target = prepare(session)
    signal = failure(cleanup_kind, 'classified cleanup', 111)
    owner = journal.DurableJournal if resource == 'journal' else filesystem.ScratchRenameSession
    close, calls = owner.close, []
    def fail_close(current):
        close(current)
        raise signal
    def fail_syscall(*args):
        calls.append(1)
        raise OSError(code, 'classified syscall failure')
    monkeypatch.setattr(owner, 'close', fail_close)
    monkeypatch.setattr(filesystem, '_rename_noreplace', fail_syscall)
    with pytest.raises(BaseException) as error:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert error.value is signal and type(signal.__cause__) is getattr(coordination, stop)
    # The interruption wins propagation, but the spent session keeps the primary STOP.
    with pytest.raises(coordination.ScratchCoordinationStopped) as again:
        session.execute(manifest, manifest.payload.actions[0], action_index=0)
    assert (type(again.value).__name__, again.value.phase) == (stop, 'RENAME')
    assert calls == [1] and source.exists() and not target.exists()
    monkeypatch.undo()
    assert [item.state for item in journal.load_journal(session.journal_path, manifest).entries] == ['PENDING']
