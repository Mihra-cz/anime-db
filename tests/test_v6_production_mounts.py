"""Production CIFS admission binds one visible share without widening scratch."""
import os
from pathlib import Path

import pytest

from app import target_execution_mounts as mounts


SHARE = '//synology/Anime'
ROOT = Path('/media/anime')


def record(number, parent, device, point, *, source=SHARE, fs_root='/',
           fstype='cifs', mode='rw'):
    """Use the same complete record parser as runtime, including raw binding."""
    def escaped(value):
        return str(value).replace('\\', '\\134').replace(' ', '\\040').replace('\t', '\\011').replace('\n', '\\012')
    raw = (f'{number} {parent} {device[0]}:{device[1]} {escaped(fs_root)} {escaped(point)} '
        f'{mode},relatime - {fstype} {escaped(source)} {mode}\n')
    return mounts._parse_mounts(raw)[0]


def namespace(root=ROOT, *, mode='rw', fs_root='/'):
    host = record(1, 1, (8, 1), '/', source='/dev/nvme0n1p1', fstype='ext4')
    media = record(42, 1, (0, 42), root, fs_root=fs_root, mode=mode)
    return host, media


def admit(records, *, root=ROOT, fd=None, share=SHARE, share_root='/', writable=True):
    return mounts._check_production_mount(root, fd, tuple(records), share=share,
        share_root=share_root, writable=writable)


def test_ro_admission_uses_mountinfo_without_filesystem_access(monkeypatch):
    records = namespace(mode='ro')
    def no_access(*args, **kwargs):
        pytest.fail('fd-less ordinary admission accessed the media filesystem')
    with monkeypatch.context() as guard:
        guard.setattr(mounts.os, 'fstat', no_access)
        guard.setattr(mounts.os, 'fstatvfs', no_access)
        guard.setattr(Path, 'stat', no_access)
        result = admit(records, writable=False)
    assert result.mount_id == 42
    assert result.source == '//synology/Anime'


def test_live_rw_root_mount_accepts_original_descriptor(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        host, _ = namespace(root=tmp_path)
        media = record(mounts._mount_id(fd), 1, (os.major(info.st_dev), os.minor(info.st_dev)), tmp_path)
        assert admit((host, media), root=tmp_path, fd=fd).mount_id == mounts._mount_id(fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize('source,fs_root,approved_subtree', [
    ('//other-server/Anime', '/', '/'),
    ('//synology/AnimeDB-V6-Probe', '/', '/'),
    ('//synology/AnimeOther', '/', '/'),
    (SHARE, '/Different', '/'),
    (SHARE, '/', '/Expected'),
])
def test_wrong_server_share_or_subtree_is_rejected(source, fs_root, approved_subtree):
    host, media = namespace()
    media = record(media.mount_id, media.parent_id, media.device, media.point,
        source=source, fs_root=fs_root)
    with pytest.raises(ValueError, match='server/share/subtree'):
        admit((host, media), share_root=approved_subtree)


def test_library_subdirectory_must_match_approved_share_projection():
    records = namespace(root=Path('/media/share'), fs_root='/Exports')
    assert admit(records, root=Path('/media/share/Anime'), share_root='/Exports/Anime').mount_id == 42
    with pytest.raises(ValueError, match='server/share/subtree'):
        admit(records, root=Path('/media/share/Anime'), share_root='/Anime')


@pytest.mark.parametrize('fstype,source', [('ext4', '/dev/nvme0n1p2'), ('cifs', '//other/Share')])
def test_nested_mount_is_rejected_even_on_another_device(fstype, source):
    records = namespace()
    nested = record(43, 42, (0, 99), ROOT / 'Season 01', source=source, fstype=fstype)
    with pytest.raises(ValueError, match='inside production library'):
        admit(records + (nested,))


@pytest.mark.parametrize('device', [(0, 42), (0, 99)])
@pytest.mark.parametrize('alias_root', ['/', '/Season01'])
def test_same_share_aliases_are_rejected_even_with_different_devices(device, alias_root):
    records = namespace()
    alias = record(43, 1, device, '/mnt/another-anime-view', fs_root=alias_root)
    with pytest.raises(ValueError, match='visible mount alias'):
        admit(records + (alias,))


def test_same_share_disjoint_subtrees_do_not_alias_library():
    records = namespace(fs_root='/Library')
    unrelated = record(43, 1, (0, 99), '/mnt/unrelated', fs_root='/Elsewhere')
    assert admit(records + (unrelated,), share_root='/Library').mount_id == 42


def test_different_share_on_different_device_is_not_library_alias():
    records = namespace()
    probe = record(43, 1, (0, 99), '/mnt/probe', source='//synology/AnimeDB-V6-Probe')
    assert admit(records + (probe,)).mount_id == 42


def test_equal_depth_overlapping_mounts_are_ambiguous():
    records = namespace()
    duplicate = record(43, 1, (0, 99), ROOT)
    with pytest.raises(ValueError, match='ambiguous'):
        admit(records + (duplicate,))


def test_fdless_admission_rejects_library_hidden_by_ancestor_overmount():
    host, _ = namespace(mode='ro')
    original_parent = record(10, 1, (8, 1), '/media', source='/dev/nvme0n1p1',
        fs_root='/media', fstype='ext4')
    media = record(42, 10, (0, 42), ROOT, mode='ro')
    covering_parent = record(70, 1, (8, 2), '/media', source='/dev/nvme1n1p1', fstype='ext4')
    with pytest.raises(ValueError):
        admit((host, original_parent, media, covering_parent), writable=False)


@pytest.mark.parametrize('crossing', ['mount_id', 'device'])
def test_live_descriptor_cannot_cross_mount_or_device(tmp_path, crossing):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        host, _ = namespace(root=tmp_path)
        mount_id = mounts._mount_id(fd)
        device = (os.major(info.st_dev), os.minor(info.st_dev))
        media = record(mount_id + int(crossing == 'mount_id'), 1,
            (device[0], device[1] + 1) if crossing == 'device' else device, tmp_path)
        with pytest.raises(ValueError, match='descriptor mount/device'):
            admit((host, media), root=tmp_path, fd=fd)
    finally:
        os.close(fd)


def test_rw_execution_rejects_mountinfo_ro():
    with pytest.raises(ValueError, match='writable media mount'):
        admit(namespace(mode='ro'))


def test_rw_execution_rejects_descriptor_vfs_ro_even_when_mountinfo_says_rw(tmp_path, monkeypatch):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        host, _ = namespace(root=tmp_path)
        media = record(mounts._mount_id(fd), 1, (os.major(info.st_dev), os.minor(info.st_dev)), tmp_path)
        values = list(os.fstatvfs(fd))
        values[8] |= os.ST_RDONLY
        monkeypatch.setattr(mounts.os, 'fstatvfs', lambda checked: os.statvfs_result(values))
        with pytest.raises(ValueError, match='writable media mount'):
            admit((host, media), root=tmp_path, fd=fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize('allow_root_mount', [False, True], ids=['scratch', 'local-state'])
def test_explicit_production_cifs_does_not_widen_local_allowlist(tmp_path, allow_root_mount):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        media = record(mounts._mount_id(fd), 1, (os.major(info.st_dev), os.minor(info.st_dev)), tmp_path)
        with pytest.raises(ValueError, match='filesystem not allowed'):
            mounts._check_mounts(tmp_path, fd, (media,), allow_root_mount=allow_root_mount)
    finally:
        os.close(fd)
