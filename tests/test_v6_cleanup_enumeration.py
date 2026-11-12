"""Final E7H enumeration must not reuse a CIFS handle's stale readdir cache."""
from contextlib import contextmanager
import errno
import os

import pytest

from app import target_execution_filesystem as fs
from test_v6_cleanup_metadata import snapshot_case  # noqa: F401


@pytest.fixture
def pinned_root_case(snapshot_case):
    owner, root = snapshot_case
    opened = owner.owner._directories.open
    @contextmanager
    def pinned(path):
        if path == '.':
            yield owner.filesystem._root_fd
        else:
            with opened(path) as fd:
                yield fd
    owner.owner._directories.open = pinned
    owner.filesystem._check_fd = lambda fd: None
    return owner, root


@pytest.mark.parametrize('production,removed', [(False,True),(True,False),(True,True)])
def test_independent_enumeration_only_for_cifs_after_removals(pinned_root_case, monkeypatch, production, removed):
    owner, root = pinned_root_case
    root_fd = owner.filesystem._root_fd
    owner.owner._production_binding = {'share':'//isolated.test/share'} if production else None
    owner.removed = ['already-removed'] if removed else []
    original = os.listdir
    calls, checked = [], []
    def listdir(fd):
        calls.append(fd)
        names = original(fd)
        if fd == root_fd and production and removed:
            return names + ['already-removed']
        return names
    def check(fd):
        assert os.fstat(fd).st_dev == os.fstat(root_fd).st_dev
        checked.append(fd)
    monkeypatch.setattr(os,'listdir',listdir)
    owner.filesystem._check_fd = check
    snapshot = owner._snapshot(full=True)
    assert {entry.path for entry in snapshot} == {'.','child'}
    assert (calls[0] != root_fd) == (production and removed)
    if production and removed:
        assert calls[0] in checked
        with pytest.raises(OSError) as closed:
            os.fstat(calls[0])
        assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize('name', ['already-removed','foreign-missing.mkv'])
def test_no_unproven_enoent_is_ignored_even_in_fresh_enumeration(pinned_root_case, monkeypatch, name):
    owner, root = pinned_root_case
    owner.removed = ['already-removed']
    original = os.listdir
    monkeypatch.setattr(os,'listdir',lambda fd:original(fd)+[name])
    with pytest.raises(FileNotFoundError) as error:
        owner._snapshot(full=True)
    assert error.value.errno == errno.ENOENT


@pytest.mark.parametrize('code', [errno.EACCES,errno.ENOENT])
def test_fresh_directory_open_failure_stops_without_fallback(pinned_root_case, monkeypatch, code):
    owner, root = pinned_root_case
    owner.removed = ['already-removed']
    original = os.open
    attempts = []
    def open_directory(path, flags, *args, **kwargs):
        if path == '.' and kwargs.get('dir_fd') is not None:
            attempts.append(path)
            raise OSError(code,'new directory handle unavailable')
        return original(path,flags,*args,**kwargs)
    monkeypatch.setattr(os,'open',open_directory)
    with pytest.raises(OSError) as error:
        owner._snapshot(full=True)
    assert error.value.errno == code and len(attempts)==1


def test_enumeration_failure_closes_independent_handle(pinned_root_case, monkeypatch):
    owner, root = pinned_root_case
    owner.removed = ['already-removed']
    attempted = []
    def unavailable(fd):
        attempted.append(fd)
        raise PermissionError(errno.EACCES,'enumeration unavailable')
    monkeypatch.setattr(os,'listdir',unavailable)
    with pytest.raises(PermissionError):
        owner._snapshot(full=True)
    assert attempted[0] != owner.filesystem._root_fd
    with pytest.raises(OSError) as closed:
        os.fstat(attempted[0])
    assert closed.value.errno == errno.EBADF


def test_independent_handle_identity_mismatch_stops(pinned_root_case, monkeypatch):
    owner, root = pinned_root_case
    owner.removed = ['already-removed']
    root_fd = owner.filesystem._root_fd
    original = os.fstat
    fresh = []
    owner.filesystem._check_fd = fresh.append
    def wrong_inode(fd):
        info = original(fd)
        if fd in fresh:
            values = list(info); values[1] += 1
            return os.stat_result(values)
        return info
    with monkeypatch.context() as patch:
        patch.setattr(os,'fstat',wrong_inode)
        with pytest.raises(fs.RenameRejected,match='enumeration directory identity changed'):
            owner._snapshot(full=True)
    assert fresh and fresh[0] != root_fd
    with pytest.raises(OSError) as closed:
        original(fresh[0])
    assert closed.value.errno == errno.EBADF
