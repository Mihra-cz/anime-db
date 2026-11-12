"""Server-returned E7H stat evidence, exact comparisons, and safe diagnostics."""
from contextlib import contextmanager
from dataclasses import replace
import ctypes
import errno
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app import target_execution_cleanup as cleanup
from app import target_execution_filesystem as fs


@pytest.mark.parametrize('name', [None, 'child'])
def test_statx_returns_exact_server_values_without_cached_stat(tmp_path, monkeypatch, name):
    def statx(fd, path, flags, mask, result):
        assert fd == 123
        assert path == (b'' if name is None else b'child')
        assert flags == (0x1000 if name is None else 0x100) | 0x2000
        value = ctypes.cast(result, ctypes.POINTER(fs._Statx)).contents
        value.mask = mask
        value.mode, value.ino, value.size = 0o40750, 456, 4096
        value.nlink, value.uid, value.gid = 3, 1000, 1001
        value.dev_major, value.dev_minor = 8, 5
        value.mtime.seconds, value.mtime.nanoseconds = 1794400000, 123456700
        value.ctime.seconds, value.ctime.nanoseconds = 1794400001, 987654300
        return 0
    monkeypatch.setattr(fs, '_load_statx', lambda: statx)
    def cached(*args, **kwargs):
        raise AssertionError('cached stat must not supply snapshot values')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'stat', cached)
        patch.setattr(os, 'fstat', cached)
        value = fs._sync_file_metadata(123, name=name)
    assert value.st_dev == os.makedev(8, 5)
    assert (value.st_ino, value.st_mode, value.st_size, value.st_nlink) == (456, 0o40750, 4096, 3)
    assert (value.st_uid, value.st_gid) == (1000, 1001)
    assert value.st_mtime_ns == 1794400000123456700
    assert value.st_ctime_ns == 1794400001987654300


@pytest.fixture
def snapshot_case(tmp_path):
    root = tmp_path / 'new-root'
    root.mkdir(mode=0o700)
    child = root / 'child'
    child.mkdir(mode=0o700)
    with fs._held_evidence_fd(os.open(root, fs._DIRECTORY)) as fd:
        filesystem = SimpleNamespace(_root_fd=fd, _check_root=lambda: None,
            _parent_binding=lambda *args: None)
        @contextmanager
        def opened(path):
            with fs._held_evidence_fd(os.open(root if path == '.' else root/path, fs._DIRECTORY)) as held:
                yield held
        value = object.__new__(cleanup._Cleanup)
        value.owner = SimpleNamespace(_production_binding={'share': '//isolated.test/share'},
            _phase='CLEANUP', _directories=SimpleNamespace(open=opened),
            _database=SimpleNamespace(_initial_snapshot=SimpleNamespace(entries=())))
        value.filesystem = filesystem
        value.manifest = SimpleNamespace(payload=SimpleNamespace(actions=()))
        value.removed = []
        value.pins = {'.': fs._identity(os.fstat(fd)), 'child': fs._identity(child.stat())}
        yield value, root


@pytest.mark.parametrize('production', [False, True])
def test_snapshot_uses_server_values_only_for_production_binding(snapshot_case, monkeypatch, production):
    value, root = snapshot_case
    value.owner._production_binding = {'share': '//isolated.test/share'} if production else None
    real_stat = os.fstat
    expected = cleanup._entry('.', 'directory', real_stat(value.filesystem._root_fd))
    def stale(fd):
        original = real_stat(fd)
        return os.stat_result(original, {'st_mtime_ns':original.st_mtime_ns-100,
            'st_ctime_ns':original.st_ctime_ns-100})
    monkeypatch.setattr(os, 'fstat', stale)
    actual = {entry.path:entry for entry in value._snapshot(full=True)}['.']
    assert actual == (expected if production else replace(expected,
        mtime_ns=expected.mtime_ns-100, ctime_ns=expected.ctime_ns-100))


@pytest.mark.parametrize('unavailable', [None, 'child'])
def test_snapshot_does_not_fall_back_when_statx_fails(snapshot_case, monkeypatch, unavailable):
    value, root = snapshot_case
    original = fs._sync_file_metadata
    def sync(fd, name=None):
        if name == unavailable:
            raise OSError(errno.EOPNOTSUPP, 'synchronization unavailable')
        return original(fd, name=name)
    monkeypatch.setattr(fs, '_sync_file_metadata', sync)
    with pytest.raises(OSError):
        value._snapshot(full=True)


def test_symlink_remains_rejected_without_following_it(snapshot_case):
    value, root = snapshot_case
    (root / 'unapproved-link').symlink_to(root.parent/'nonexistent-private-target')
    with pytest.raises(fs.RenameRejected, match='unknown content, symlink'):
        value._snapshot(full=True)


def test_snapshot_difference_private_diagnostic_identifies_fields_and_values():
    private = 'private-title/secret-directory'
    expected = cleanup.CleanupEntry(private, 'directory', 1, 2, 0o40700, 1000, 1000, 2, 4096, 100, 200)
    actual = replace(expected, mtime_ns=300, ctime_ns=400)
    error = cleanup._snapshot_difference_error('STOP: filesystem differs from approved cleanup delta',
        (expected,), (actual,), phase='CLEANUP')
    detail = json.loads(str(error).split('; CLEANUP_MISMATCH_JSON=',1)[1])
    assert detail['phase'] == 'CLEANUP' and detail['different_records'] == 1
    assert detail['records'] == [{'record_sha256':hashlib.sha256(private.encode()).hexdigest(),
        'differences': {'mtime_ns':{'expected':100,'actual':300},
                        'ctime_ns':{'expected':200,'actual':400}}}]
    assert private not in str(error)
    assert error.cleanup_mismatch == detail


@pytest.mark.parametrize('field', ['device','inode','mode','uid','gid','nlink','size','mtime_ns','ctime_ns'])
def test_snapshot_diagnostic_preserves_every_numeric_difference(field):
    expected = cleanup.CleanupEntry('private/path', 'directory', 1, 2, 0o40700, 1000, 1000, 2, 4096, 100, 200)
    changed = replace(expected, **{field:getattr(expected,field)+1})
    error = cleanup._snapshot_difference_error('STOP', (expected,), (changed,), phase='CLEANUP')
    assert error.cleanup_mismatch['records'][0]['differences'] == {
        field:{'expected':getattr(expected,field),'actual':getattr(expected,field)+1}}
    assert 'private/path' not in str(error)


def test_snapshot_diagnostic_is_bounded_and_redacts_nonnumeric_fields():
    expected = tuple(cleanup.CleanupEntry(f'private/{n}', 'directory', 1, 2, 0o40700, 1000, 1000, 2, 4096, 100, 200)
        for n in range(30))
    changed = tuple(replace(row, kind='private-kind', ctime_ns=300) for row in expected)
    error = cleanup._snapshot_difference_error('STOP', expected, changed, phase='CLEANUP')
    assert error.cleanup_mismatch['different_records'] == 30
    assert error.cleanup_mismatch['omitted_records'] == 22
    assert len(error.cleanup_mismatch['records']) == 8 and len(str(error)) < 8192
    assert 'private' not in str(error)
    assert error.cleanup_mismatch['records'][0]['differences']['kind'] == {}


def test_snapshot_diagnostic_distinguishes_added_and_missing_records():
    expected = cleanup.CleanupEntry('private/before', 'directory', 1, 2, 0o40700, 1000, 1000, 2, 4096, 100, 200)
    actual = replace(expected, path='private/after')
    error = cleanup._snapshot_difference_error('STOP', (expected,), (actual,), phase='CLEANUP')
    assert error.cleanup_mismatch['different_records'] == 2
    assert [row['differences'] for row in error.cleanup_mismatch['records']] == [
        {'presence':{'expected':0,'actual':1}}, {'presence':{'expected':1,'actual':0}}]


@pytest.mark.parametrize('missing', [0x1,0x2,0x4,0x8,0x10,0x40,0x80,0x100,0x200])
def test_named_directory_statx_requires_every_snapshot_field(monkeypatch, missing):
    def statx(fd,path,flags,mask,result):
        ctypes.cast(result, ctypes.POINTER(fs._Statx)).contents.mask = mask & ~missing
        return 0
    monkeypatch.setattr(fs, '_load_statx', lambda:statx)
    with pytest.raises(OSError, match='metadata incomplete'):
        fs._sync_file_metadata(123, name='child')


@pytest.mark.parametrize('case', ['production','scratch','close_failure','unconfirmed_unlink'])
def test_rmdir_delta_requires_confirmed_unlink_and_candidate_close(tmp_path, monkeypatch, case):
    parent = tmp_path/'parent'
    child = parent/'child'
    child.mkdir(parents=True)
    value = object.__new__(cleanup._Cleanup)
    value.owner = SimpleNamespace(_production_binding=None if case == 'scratch' else {'share':'//isolated.test/share'},
        _directories=SimpleNamespace(removed=set()))
    value.filesystem = SimpleNamespace(_parent_binding=lambda *args:None)
    value.removed = []
    states, released, absent = [], [], []
    original_absent = fs._absent
    def confirm_absent(fd, name):
        original_absent(fd, name)
        absent.append(True)
    monkeypatch.setattr(fs, '_absent', confirm_absent)
    value._append = lambda state,**kwargs:states.append(state)
    value._check = lambda **kwargs:None
    value._expected_entries = {'parent':cleanup._entry('parent','directory',parent.stat()),
        'parent/child':cleanup._entry('parent/child','directory',child.stat())}
    with fs._held_evidence_fd(os.open(parent,fs._DIRECTORY)) as parent_fd:
        child_fd = os.open(child,fs._DIRECTORY)
        real_stat = os.fstat
        def stat(fd):
            assert case == 'scratch' or released, 'server delta must be acquired after candidate close'
            assert absent, 'parent checkpoint must follow confirmed named absence'
            return real_stat(fd)
        value._stat = stat
        def release():
            released.append(True)
            if case == 'close_failure':
                raise OSError(errno.EIO,'close unconfirmed')
            os.close(child_fd)
        def unconfirmed(fd):
            info = real_stat(fd)
            if fd == child_fd:
                raw=list(info);raw[3]=2
                return os.stat_result(raw)
            return info
        try:
            with monkeypatch.context() as patch:
                if case == 'unconfirmed_unlink':
                    patch.setattr(os,'fstat',unconfirmed)
                if case in {'close_failure','unconfirmed_unlink'}:
                    with pytest.raises((OSError,fs.RenameRejected)):
                        value._remove('parent/child',('parent','child'),'parent',parent_fd,child_fd,
                            release_candidate=release)
                    assert states == ['INTENT']
                else:
                    value._remove('parent/child',('parent','child'),'parent',parent_fd,child_fd,
                        release_candidate=release)
                    assert states == ['INTENT','VERIFIED']
                    assert bool(released) == (case == 'production')
                    assert value._expected_entries['parent'].mtime_ns == real_stat(parent_fd).st_mtime_ns
                assert not child.exists()
        finally:
            if case != 'production':
                os.close(child_fd)
