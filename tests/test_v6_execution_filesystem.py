"""E2A writes only newly created scratch trees inside pytest tmp_path.

Mount and fault injection tests establish rejection logic, not NAS capabilities.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import errno
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import stat
import threading

import pytest

from app.target_execution_manifest import manifest_to_json
from test_v6_execution_manifest import fixture, make


def api():
    assert importlib.util.find_spec('app.target_execution_filesystem') is not None, \
        'E2A filesystem primitive is missing'
    return importlib.import_module('app.target_execution_filesystem')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True).encode()).hexdigest()


def seal(manifest, actions=None, **changes):
    """Explicit synthetic manifest; never a production artifact."""
    actions = tuple(sorted(actions or manifest.payload.actions,
        key=lambda a: (a.source, a.object_kind, str(a.object_id))))
    directories = {str(p) for a in actions for p in PurePosixPath(a.target).parents if str(p) != '.'}
    payload = replace(manifest.payload, actions=actions,
        directories=tuple(sorted(directories, key=lambda p: (len(PurePosixPath(p).parts), p))),
        logical_move_bytes=sum(a.expected_size for a in actions if a.action != 'KEEP'), **changes)
    return replace(manifest, payload=payload, manifest_id=digest(asdict(payload)))


def changed_action(manifest, **changes):
    action = replace(manifest.payload.actions[0], **changes)
    return replace(action, action_id=digest((action.object_kind, action.object_id,
        action.action, action.source, action.target)))


def prepare(session, *, action='MOVE'):
    source = session.root / 'old/source.mkv'
    source.parent.mkdir(mode=0o700)
    source.write_bytes(b'original scratch video')
    source.chmod(0o600)
    info = source.stat()
    ctx, snap = fixture()
    snap = replace(snap, root=str(session.root),
        entries=(replace(snap.entries[0], size=info.st_size, mtime_ns=info.st_mtime_ns),))
    manifest = make(ctx, snap, mount_identity=session.mount_identity)
    target = 'new/target.mkv' if action == 'MOVE' else 'Duplicates/new/target.mkv'
    if action == 'KEEP':
        target = 'old/source.mkv'
    manifest = seal(manifest, (changed_action(manifest, action=action, target=target,
        expected_target_absent=action != 'KEEP'),))
    (session.root / target).parent.mkdir(parents=True, exist_ok=True)
    for directory in (session.root / target).parents:
        if directory == session.root:
            break
        directory.chmod(0o700)
    return manifest, source, session.root / target


@pytest.fixture
def case(tmp_path):
    with api().ScratchRenameSession.create(tmp_path) as session:
        yield session, *prepare(session)


@pytest.mark.parametrize('action', ['MOVE', 'QUARANTINE'])
def test_rename_preserves_inode_bytes_and_manifest_and_confirms_barriers(tmp_path, action):
    fs = api()
    with fs.ScratchRenameSession.create(tmp_path) as session:
        manifest, source, target = prepare(session, action=action)
        before, raw = source.stat(), manifest_to_json(manifest)
        result = session.rename(manifest, manifest.payload.actions[0])
        assert result.outcome == 'DURABLE'
        assert result.syscall_succeeded is True
        assert result.durability_confirmed is True
        assert result.manifest_id == manifest.manifest_id
        assert result.action_id == manifest.payload.actions[0].action_id
        assert not source.exists()
        assert target.read_bytes() == b'original scratch video'
        assert (target.stat().st_dev, target.stat().st_ino) == (before.st_dev, before.st_ino)
        assert target.stat().st_mtime_ns == before.st_mtime_ns
        assert manifest_to_json(manifest) == raw


def test_keep_checks_identity_without_syscall_or_fsync(tmp_path, monkeypatch):
    fs = api()
    with fs.ScratchRenameSession.create(tmp_path) as session:
        manifest, source, target = prepare(session, action='KEEP')
        monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('KEEP renamed'))
        monkeypatch.setattr(fs.os, 'fsync', lambda *a: pytest.fail('KEEP synchronized'))
        result = session.rename(manifest, manifest.payload.actions[0])
        assert result.outcome == 'KEEP'
        assert result.syscall_succeeded is False
        assert result.durability_confirmed is False
        assert source == target and source.read_bytes() == b'original scratch video'


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_interrupted_syscall_preserves_process_signal_and_blocks_session(case, monkeypatch, interruption):
    fs = api()
    session, manifest, source, target = case
    signal = interruption('injected interruption')
    def interrupt(*args):
        raise signal
    monkeypatch.setattr(fs, '_rename_noreplace', interrupt)
    with pytest.raises(interruption) as error:
        session.rename(manifest, manifest.payload.actions[0])
    assert error.value is signal
    with pytest.raises(fs.RenameReconciliationRequired):
        session.rename(manifest, manifest.payload.actions[0])
    assert source.exists() and not target.exists()


@pytest.mark.parametrize('kind', ['file', 'symlink', 'dangling_symlink', 'directory'])
def test_existing_target_is_never_replaced(case, kind):
    fs = api()
    session, manifest, source, target = case
    if kind == 'file':
        target.write_bytes(b'preserve target')
    elif kind == 'directory':
        target.mkdir()
    else:
        target.symlink_to(source if kind == 'symlink' else target.parent / 'missing')
    before = target.lstat()
    with pytest.raises(fs.RenameRejected) as error:
        session.rename(manifest, manifest.payload.actions[0])
    assert error.value.errno == errno.EEXIST
    assert source.read_bytes() == b'original scratch video'
    assert target.lstat().st_ino == before.st_ino
    if kind == 'file':
        assert target.read_bytes() == b'preserve target'


@pytest.mark.parametrize('kind', ['source_symlink', 'source_fifo', 'source_directory',
    'source_hardlink', 'source_missing', 'target_parent_missing', 'source_parent_symlink',
    'target_parent_symlink', 'size', 'mtime', 'unsafe_parent', 'unsafe_source'])
def test_source_and_parent_rejections_preserve_all_assets(case, tmp_path, kind):
    fs = api()
    session, manifest, source, target = case
    victim = tmp_path / 'victim'
    victim.write_bytes(b'outside scratch root')
    if kind.startswith('source_') and kind in {'source_symlink', 'source_fifo', 'source_directory', 'source_missing'}:
        source.unlink()
        if kind == 'source_symlink':
            source.symlink_to(victim)
        elif kind == 'source_fifo':
            os.mkfifo(source)
        elif kind == 'source_directory':
            source.mkdir()
    elif kind == 'source_hardlink':
        os.link(source, tmp_path / 'alias')
    elif kind == 'target_parent_missing':
        target.parent.rmdir()
    elif kind.endswith('parent_symlink'):
        parent = source.parent if kind.startswith('source') else target.parent
        displaced = session.root / 'displaced'
        parent.rename(displaced)
        parent.symlink_to(displaced, target_is_directory=True)
    elif kind == 'size':
        source.write_bytes(b'changed')
    elif kind == 'mtime':
        os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1))
    elif kind == 'unsafe_parent':
        target.parent.chmod(0o777)
    elif kind == 'unsafe_source':
        source.chmod(0o666)
    before = source.lstat() if source.exists() or source.is_symlink() else None
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, manifest.payload.actions[0])
    assert not target.exists()
    assert victim.read_bytes() == b'outside scratch root'
    if before:
        assert source.lstat().st_ino == before.st_ino


@pytest.mark.parametrize('path', ['../escape', '/tmp/escape', 'old/../escape',
    'old//file', 'old/./file', 'old\\file', 'old/\x00file', '#recycle/file'])
def test_unsafe_paths_fail_before_syscall(case, path, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    manifest = seal(manifest, (changed_action(manifest, source=path),))
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('unsafe path renamed'))
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, manifest.payload.actions[0])
    assert source.read_bytes() == b'original scratch video'
    assert not target.exists()


@pytest.mark.parametrize('kind', ['hash', 'action_id', 'foreign_action', 'root', 'typed_size',
    'DELETE', 'REVIEW', 'criticality_review', 'generation_diagnostics', 'dependencies', 'mount_identity'])
def test_invalid_manifest_or_action_cannot_authorize_rename(case, kind, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    action = manifest.payload.actions[0]
    if kind == 'hash':
        manifest = replace(manifest, manifest_id='0' * 64)
    elif kind == 'action_id':
        action = replace(action, action_id='0' * 64)
        manifest = seal(manifest, (action,))
    elif kind == 'foreign_action':
        action = replace(action, expected_size=action.expected_size + 1)
    elif kind == 'root':
        manifest = seal(manifest, library_root=str(session.root.parent))
    elif kind == 'typed_size':
        action = replace(action, expected_size=True)
        manifest = seal(manifest, (action,))
    elif kind in {'DELETE', 'REVIEW'}:
        action = changed_action(manifest, action=kind)
        manifest = seal(manifest, (action,))
    elif kind == 'criticality_review':
        action = replace(action, criticality='REVIEW', failure_policy='REVIEW')
        manifest = seal(manifest, (action,))
    elif kind == 'generation_diagnostics':
        manifest = seal(manifest, generation_diagnostics=('not_actionable',))
    elif kind == 'mount_identity':
        manifest = seal(manifest, mount_identity='UNVERIFIED')
    else:
        action = replace(action, depends_on=(action.action_id,))
        manifest = seal(manifest, (action,))
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('invalid identity renamed'))
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, action)
    assert source.read_bytes() == b'original scratch video'
    assert not target.exists()


@pytest.mark.parametrize('code', [errno.EEXIST, errno.EXDEV, errno.ENOSYS, errno.EOPNOTSUPP,
    errno.EINVAL, errno.EACCES, errno.EPERM, errno.EROFS])
def test_known_syscall_failure_stops_and_preserves_assets_without_fallback(case, code, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    calls = []
    def fail(*args):
        calls.append(args)
        raise OSError(code, 'injected syscall failure')
    monkeypatch.setattr(fs, '_rename_noreplace', fail)
    monkeypatch.setattr(fs.os, 'rename', lambda *a, **k: pytest.fail('rename fallback'))
    monkeypatch.setattr(fs.os, 'replace', lambda *a, **k: pytest.fail('replace fallback'))
    with pytest.raises(fs.RenameRejected) as error:
        session.rename(manifest, manifest.payload.actions[0])
    assert error.value.errno == code
    assert len(calls) == 1
    assert source.read_bytes() == b'original scratch video' and not target.exists()


@pytest.mark.parametrize('code', [errno.EIO, errno.EINTR, errno.ETIMEDOUT])
@pytest.mark.parametrize('applied', [False, True])
def test_ambiguous_syscall_requires_reconciliation_and_never_retries(case, code, applied, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    real = fs._rename_noreplace
    calls = []
    def fail(*args):
        calls.append(args)
        if applied:
            real(*args)
        raise OSError(code, 'injected uncertain acknowledgement')
    monkeypatch.setattr(fs, '_rename_noreplace', fail)
    for _ in range(2):
        with pytest.raises(fs.RenameReconciliationRequired) as error:
            session.rename(manifest, manifest.payload.actions[0])
        assert error.value.durability_confirmed is False
    assert len(calls) == 1
    assert source.exists() is (not applied)
    assert target.exists() is applied


@pytest.mark.parametrize('barrier', [1, 2, 3])
def test_fsync_failure_after_rename_reports_applied_and_poisoned_session(case, barrier, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    calls = []
    real = fs.os.fsync
    def fail(fd):
        calls.append(fd)
        if len(calls) == barrier:
            raise OSError(errno.EIO, 'injected fsync failure')
        real(fd)
    monkeypatch.setattr(fs.os, 'fsync', fail)
    with pytest.raises(fs.RenameReconciliationRequired) as error:
        session.rename(manifest, manifest.payload.actions[0])
    assert error.value.syscall_succeeded is True
    assert error.value.durability_confirmed is False
    assert not source.exists() and target.read_bytes() == b'original scratch video'
    with pytest.raises(fs.RenameReconciliationRequired):
        session.rename(manifest, manifest.payload.actions[0])
    assert len(calls) == barrier


def test_success_synchronizes_file_and_both_distinct_parents(case, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    synced = []
    real = fs.os.fsync
    def sync(fd):
        info = os.fstat(fd)
        synced.append((info.st_ino, stat.S_ISDIR(info.st_mode)))
        real(fd)
    source_ino, old_parent, new_parent = source.stat().st_ino, source.parent.stat().st_ino, target.parent.stat().st_ino
    monkeypatch.setattr(fs.os, 'fsync', sync)
    session.rename(manifest, manifest.payload.actions[0])
    assert synced == [(source_ino, False), (new_parent, True), (old_parent, True)]


def test_target_race_is_stopped_by_kernel_noreplace(case, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    real = fs._rename_noreplace
    def competing_create(*args):
        target.write_bytes(b'concurrent target')
        real(*args)
    monkeypatch.setattr(fs, '_rename_noreplace', competing_create)
    with pytest.raises(fs.RenameRejected) as error:
        session.rename(manifest, manifest.payload.actions[0])
    assert error.value.errno == errno.EEXIST
    assert source.read_bytes() == b'original scratch video'
    assert target.read_bytes() == b'concurrent target'


def test_two_real_concurrent_syscalls_never_overwrite(tmp_path):
    fs = api()
    (tmp_path / 'a').write_bytes(b'a')
    (tmp_path / 'b').write_bytes(b'b')
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    barrier = threading.Barrier(2)
    def run(name):
        barrier.wait()
        try:
            fs._rename_noreplace(fd, name, fd, 'target')
            return name, 0
        except OSError as error:
            return name, error.errno
    try:
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(run, ('a', 'b')))
    finally:
        os.close(fd)
    assert sorted(code for _, code in outcomes) == [0, errno.EEXIST]
    winner = next(name for name, code in outcomes if code == 0)
    loser = next(name for name, code in outcomes if code != 0)
    assert (tmp_path / 'target').read_bytes() == winner.encode()
    assert (tmp_path / loser).read_bytes() == loser.encode()


def test_no_open_existing_root_or_production_mode(tmp_path):
    fs = api()
    existing = tmp_path / 'existing'
    existing.mkdir(mode=0o700)
    (existing / 'sentinel').write_bytes(b'preserve')
    with pytest.raises(TypeError):
        fs.ScratchRenameSession(existing)
    with pytest.raises(fs.RenameRejected):
        fs.ScratchRenameSession.create(Path('/mnt/nas-anime'))
    with fs.ScratchRenameSession.create(existing) as session:
        assert session.root.parent == existing
        assert list(session.root.iterdir()) == []
        assert (existing / 'sentinel').read_bytes() == b'preserve'


def test_session_cannot_reuse_applied_action_or_closed_root(case):
    fs = api()
    session, manifest, _, target = case
    session.rename(manifest, manifest.payload.actions[0])
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, manifest.payload.actions[0])
    session.close()
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, manifest.payload.actions[0])
    assert target.read_bytes() == b'original scratch video'


@pytest.mark.parametrize('kind', ['nested_mount', 'bind_root', 'bind_subtree',
    'different_fd_mount', 'unknown_mount', 'unreadable_mountinfo', 'changed_mountinfo'])
def test_mount_crossings_aliases_and_unverifiable_mounts_stop_before_rename(case, kind, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    mounts = fs._read_mounts()
    current = next(m for m in mounts if m.mount_id == fs._mount_id(session._root_fd))
    if kind in {'nested_mount', 'bind_root', 'bind_subtree'}:
        storage_root = current.fs_root / session.root.relative_to(current.point).as_posix()
        extra = replace(current, mount_id=max(m.mount_id for m in mounts) + 1,
            point=session.root / 'new' if kind == 'nested_mount' else session.root.parent / 'alias',
            fs_root=storage_root / 'old' if kind == 'bind_subtree' else storage_root)
        monkeypatch.setattr(fs, '_read_mounts', lambda: mounts + (extra,))
    elif kind == 'different_fd_mount':
        real = fs._mount_id
        monkeypatch.setattr(fs, '_mount_id', lambda fd: real(fd) + 1 if fd != session._root_fd else real(fd))
    elif kind == 'unknown_mount':
        monkeypatch.setattr(fs, '_read_mounts', lambda: ())
    elif kind == 'unreadable_mountinfo':
        def fail():
            raise OSError(errno.EACCES, 'injected proc access failure')
        monkeypatch.setattr(fs, '_read_mounts', fail)
    else:
        monkeypatch.setattr(fs, '_read_mounts', lambda: mounts + (replace(current,
            mount_id=max(m.mount_id for m in mounts) + 1, point=Path('/unrelated'),
            device=(0, 9999)),))
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('unsafe mount renamed'))
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, manifest.payload.actions[0])
    assert source.read_bytes() == b'original scratch video' and not target.exists()


@pytest.mark.parametrize('fstype', ['cifs', 'nfs', 'nfs4', 'virtiofs', 'fuse', 'fuse.sshfs',
    'ceph', '9p', 'UNKNOWN', 'tmpfs', 'ramfs'])
def test_scratch_filesystem_policy_is_allowlist(case, fstype, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    mounts = fs._read_mounts()
    root_id = fs._mount_id(session._root_fd)
    monkeypatch.setattr(fs, '_read_mounts', lambda: tuple(
        replace(m, fstype=fstype) if m.mount_id == root_id else m for m in mounts))
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, manifest.payload.actions[0])
    assert source.exists() and not target.exists()


def test_root_parent_and_source_bindings_are_rechecked(case, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    real = fs._check_source
    def swapped(fd, parent, name, action, expected=None):
        result = real(fd, parent, name, action, expected)
        if expected is None:
            source.rename(source.with_suffix('.original'))
            source.write_bytes(b'other inode')
        return result
    monkeypatch.setattr(fs, '_check_source', swapped)
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, manifest.payload.actions[0])
    assert source.read_bytes() == b'other inode'
    assert source.with_suffix('.original').read_bytes() == b'original scratch video'
    assert not target.exists()


def test_reused_root_descriptor_number_cannot_redirect_rename(case, tmp_path):
    fs = api()
    session, manifest, source, target = case
    # An unrelated same-mount tree mirrors the scratch layout and source stat evidence.
    other = tmp_path / 'other-tree'
    (other / 'old').mkdir(mode=0o700, parents=True)
    (other / 'new').mkdir(mode=0o700)
    other.chmod(0o700)
    decoy = other / 'old/source.mkv'
    decoy.write_bytes(source.read_bytes())
    decoy.chmod(0o600)
    os.utime(decoy, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))
    fd = os.open(other, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.dup2(fd, session._root_fd)  # Simulates descriptor-number reuse after a stray close.
    finally:
        os.close(fd)
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, manifest.payload.actions[0])
    assert decoy.read_bytes() == b'original scratch video'
    assert not (other / 'new/target.mkv').exists()
    assert not target.exists()


def test_post_syscall_identity_mismatch_is_uncertain(case, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    real = fs._rename_noreplace
    def swap(*args):
        real(*args)
        target.rename(target.with_suffix('.applied'))
        target.write_bytes(b'foreign target')
    monkeypatch.setattr(fs, '_rename_noreplace', swap)
    with pytest.raises(fs.RenameReconciliationRequired) as error:
        session.rename(manifest, manifest.payload.actions[0])
    assert error.value.syscall_succeeded is True
    assert not source.exists()
    assert target.read_bytes() == b'foreign target'
    assert target.with_suffix('.applied').read_bytes() == b'original scratch video'


def test_foreign_manifest_root_is_rejected_without_path_observation(case, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    foreign = seal(manifest, library_root=str(session.root.parent))
    monkeypatch.setattr(fs, 'manifest_to_json', lambda *a: pytest.fail('observed a foreign root'))
    with pytest.raises(fs.RenameRejected):
        session.rename(foreign, foreign.payload.actions[0])
    assert source.exists() and not target.exists()


@pytest.mark.parametrize('kind', ['nested_mount', 'bind_root', 'bind_subtree', 'bind_ancestor'])
def test_mount_alias_policy_inspects_subtrees_even_in_initial_snapshot(case, kind):
    fs = api()
    session, _, source, target = case
    mounts = fs._read_mounts()
    current = next(m for m in mounts if m.mount_id == fs._mount_id(session._root_fd))
    storage = current.fs_root / session.root.relative_to(current.point).as_posix()
    extra = replace(current, mount_id=max(m.mount_id for m in mounts) + 1,
        point=session.root / 'new' if kind == 'nested_mount' else session.root.parent / 'alias',
        fs_root=storage / 'old' if kind == 'bind_subtree' else storage.parent if kind == 'bind_ancestor' else storage)
    with pytest.raises(ValueError, match='(mount at or inside|bind mount aliases)'):
        fs._check_mounts(session.root, session._root_fd, mounts + (extra,))
    assert source.exists() and not target.exists()


@pytest.mark.parametrize('fstype', ['cifs', 'nfs', 'fuse', 'virtiofs', 'ceph', 'UNKNOWN', 'tmpfs'])
def test_unsupported_initial_mount_cannot_allocate_scratch_tree(tmp_path, fstype, monkeypatch):
    fs = api()
    mounts = fs._read_mounts()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        mount_id = fs._mount_id(fd)
    finally:
        os.close(fd)
    monkeypatch.setattr(fs, '_read_mounts', lambda: tuple(
        replace(m, fstype=fstype) if m.mount_id == mount_id else m for m in mounts))
    with pytest.raises(fs.RenameRejected):
        fs.ScratchRenameSession.create(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('kind', ['root', 'source_parent', 'target_parent'])
def test_directory_replacement_before_syscall_stops(case, kind, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    real = fs._check_source
    directory = session.root if kind == 'root' else source.parent if kind == 'source_parent' else target.parent
    displaced = directory.with_name(directory.name + '-original')
    def swap(fd, parent, name, action, expected=None):
        result = real(fd, parent, name, action, expected)
        if expected is None:
            directory.rename(displaced)
            directory.mkdir(mode=0o700)
        return result
    monkeypatch.setattr(fs, '_check_source', swap)
    monkeypatch.setattr(fs, '_rename_noreplace', lambda *a: pytest.fail('changed parent renamed'))
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, manifest.payload.actions[0])
    if kind == 'root':
        preserved = displaced / 'old/source.mkv'
    elif kind == 'source_parent':
        preserved = displaced / source.name
    else:
        preserved = source
    assert preserved.read_bytes() == b'original scratch video'
    assert not target.exists()


def test_successful_same_directory_rename_syncs_directory_once(case, monkeypatch):
    fs = api()
    session, manifest, source, _ = case
    action = changed_action(manifest, target='old/renamed.mkv')
    manifest = seal(manifest, (action,))
    real = fs.os.fsync
    synced = []
    def sync(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real(fd)
    monkeypatch.setattr(fs.os, 'fsync', sync)
    session.rename(manifest, action)
    assert synced == [False, True]
    assert not source.exists()
    assert (session.root / action.target).read_bytes() == b'original scratch video'


def test_known_errno_after_physical_move_is_still_uncertain(case, monkeypatch):
    fs = api()
    session, manifest, source, target = case
    real = fs._rename_noreplace
    def fail(*args):
        real(*args)
        raise OSError(errno.EACCES, 'injected lost acknowledgement')
    monkeypatch.setattr(fs, '_rename_noreplace', fail)
    with pytest.raises(fs.RenameReconciliationRequired):
        session.rename(manifest, manifest.payload.actions[0])
    assert not source.exists() and target.read_bytes() == b'original scratch video'


@pytest.mark.parametrize('raw', ['', 'broken', '1 2 8:5 / / rw - ext4 device',
    '1 2 8:5 / / rw - ext4 device rw\n1 2 8:5 / / rw - ext4 device rw',
    '1 2 8:5 / /bad\\999 rw - ext4 device rw'])
def test_malformed_mountinfo_is_fail_closed(raw):
    from app.target_execution_mounts import _parse_mounts
    with pytest.raises(ValueError):
        _parse_mounts(raw)


def test_mountinfo_escaped_paths_are_decoded_without_guessing():
    from app.target_execution_mounts import _parse_mounts
    record, = _parse_mounts('1 2 8:5 /a\\040b /c\\011d rw - ext4 /dev/device rw')
    assert record.fs_root == PurePosixPath('/a b')
    assert record.point == Path('/c\td')


def test_missing_libc_symbol_fails_closed_without_allocating_root(tmp_path, monkeypatch):
    fs = api()
    def unavailable():
        raise OSError(errno.ENOSYS, 'injected unavailable symbol')
    monkeypatch.setattr(fs, '_load_renameat2', unavailable)
    with pytest.raises(fs.RenameRejected) as error:
        fs.ScratchRenameSession.create(tmp_path)
    assert error.value.errno == errno.ENOSYS
    assert list(tmp_path.iterdir()) == []


def test_session_two_concurrent_actions_same_target(case):
    fs = api()
    session, manifest, source, target = case
    second = source.with_name('second.mkv')
    second.write_bytes(b'other scratch video')
    second.chmod(0o600)
    action = changed_action(manifest, source='old/second.mkv', object_id=101,
        expected_size=second.stat().st_size, expected_mtime_ns=second.stat().st_mtime_ns)
    manifest = seal(manifest, (manifest.payload.actions[0], action))
    barrier = threading.Barrier(2)
    def run(a):
        barrier.wait()
        try:
            return session.rename(manifest, a).outcome
        except fs.RenameRejected as error:
            return error.errno
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(run, manifest.payload.actions))
    assert sorted(map(str, outcomes)) == ['17', 'DURABLE']
    assert target.read_bytes() in {b'original scratch video', b'other scratch video'}
    assert sum(path.exists() for path in (source, second)) == 1


@pytest.mark.parametrize('field', ['expected_target_absent', 'expected_size', 'object_id'])
def test_equal_but_wrongly_typed_supplied_action_is_rejected(case, field):
    fs = api()
    session, manifest, source, target = case
    sealed = manifest.payload.actions[0]
    value = 1 if field == 'expected_target_absent' else float(getattr(sealed, field))
    supplied = replace(sealed, **{field: value})
    assert supplied == sealed  # Python bool/int/float equality must not waive strict identity.
    with pytest.raises(fs.RenameRejected):
        session.rename(manifest, supplied)
    assert source.read_bytes() == b'original scratch video'
    assert not target.exists()


def test_initial_readonly_mount_cannot_allocate_scratch_tree(tmp_path, monkeypatch):
    fs = api()
    mounts = fs._read_mounts()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        mount_id = fs._mount_id(fd)
    finally:
        os.close(fd)
    monkeypatch.setattr(fs, '_read_mounts', lambda: tuple(
        replace(m, options=('ro',)) if m.mount_id == mount_id else m for m in mounts))
    with pytest.raises(fs.RenameRejected):
        fs.ScratchRenameSession.create(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_scratch_allocation_rejects_symlinked_ancestor(tmp_path):
    fs = api()
    real = tmp_path / 'real'
    real.mkdir(mode=0o700)
    nested = real / 'nested'
    nested.mkdir(mode=0o700)
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(fs.RenameRejected):
        fs.ScratchRenameSession.create(alias / 'nested')
    assert list(nested.iterdir()) == []
