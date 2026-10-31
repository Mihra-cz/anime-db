"""Only isolated local directories; these tests never access an SMB server."""
import errno
import json
import os
import stat

import pytest

from app.tools import smb_probe


def test_local_probe_preserves_content_and_reports_identity(tmp_path, capsys):
    assert smb_probe.main(['--work-dir', str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    checks = report['checks']
    assert all(row['status'] == 'PASS' for name, row in checks.items()
        if name != 'power_loss_durability')
    assert checks['power_loss_durability']['status'] == 'NEOVĚŘENO'
    assert checks['reject_existing_target']['errno'] == errno.EEXIST
    assert (tmp_path / 'target/moved.bin').stat().st_size == 65536
    assert not (tmp_path / 'source/renamed.bin').exists()
    stats = report['observations']
    assert stats['before']['st_ino'] == stats['reopened_after_cross']['st_ino']
    assert stats['before']['st_dev'] == stats['reopened_after_cross']['st_dev']
    assert report['production_execution_authorized'] is False
    # A second invocation must retain the original artifacts untouched.
    artifacts = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert smb_probe.main(['--work-dir', str(tmp_path)]) == 2
    assert all(p.read_bytes() == data for p, data in artifacts.items())


@pytest.mark.parametrize('kind', ['nonempty', 'symlink', 'forbidden'])
def test_unsafe_work_directory_is_refused_before_writes(tmp_path, monkeypatch, kind):
    if kind == 'nonempty':
        (tmp_path / 'existing-library.mkv').write_bytes(b'leave this file alone')
        work = tmp_path
    elif kind == 'symlink':
        target = tmp_path / 'empty'
        target.mkdir(mode=0o700)
        work = tmp_path / 'alias'
        work.symlink_to(target, target_is_directory=True)
    else:
        work = '/mnt/nas-anime/diagnostic'
        monkeypatch.setattr(smb_probe.fs, '_open_absolute',
            lambda *_: pytest.fail('forbidden NAS path was accessed'))
    before = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*')}
    report = smb_probe.probe(work)
    assert report['checks']['work_directory']['status'] == 'FAIL'
    assert report['exit_code'] == 2
    assert {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*')} == before
    if kind == 'nonempty':
        assert (tmp_path / 'existing-library.mkv').read_bytes() == b'leave this file alone'


def test_directory_fsync_errors_are_explicit_without_hiding_rename_results(tmp_path, monkeypatch):
    fsync = os.fsync
    def unsupported(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, 'injected directory fsync refusal')
        fsync(fd)
    monkeypatch.setattr(smb_probe.os, 'fsync', unsupported)
    report = smb_probe.probe(tmp_path)
    assert report['exit_code'] == 1
    assert report['checks']['move_between_directories']['status'] == 'PASS'
    assert report['checks']['content_preserved']['status'] == 'PASS'
    result = report['checks']['fsync_destination_directory']
    assert (result['status'], result['errno'], result['errno_name']) == ('FAIL', errno.EINVAL, 'EINVAL')


def test_unexpected_overwrite_stops_the_probe(tmp_path, monkeypatch):
    rename = smb_probe.fs._rename_noreplace
    def broken(source_fd, source, target_fd, target):
        if target == 'collision.bin':
            os.rename(source, target, src_dir_fd=source_fd, dst_dir_fd=target_fd)
        else:
            rename(source_fd, source, target_fd, target)
    monkeypatch.setattr(smb_probe.fs, '_rename_noreplace', broken)
    report = smb_probe.probe(tmp_path)
    assert report['exit_code'] == 1
    assert report['checks']['reject_existing_target']['status'] == 'FAIL'
    assert report['checks']['move_between_directories']['status'] == 'NEOVĚŘENO'


def test_production_share_alias_is_refused_before_opening_work_directory(monkeypatch):
    from dataclasses import replace
    from pathlib import Path, PurePosixPath
    from app.target_execution_mounts import _parse_mounts
    production, alias = _parse_mounts(
        '10 1 0:80 / /mnt/nas-anime rw - cifs //synology/anime rw\n'
        '11 1 0:80 /Season /probe rw - cifs //synology/anime rw\n')
    monkeypatch.setattr(smb_probe.fs, '_read_mounts', lambda: (production, alias))
    monkeypatch.setattr(smb_probe.fs, '_open_absolute',
        lambda *_: pytest.fail('identified production alias was opened'))
    report = smb_probe.probe('/probe/empty')
    assert report['exit_code'] == 2
    assert report['checks']['prepare_test_files']['status'] == 'NEOVĚŘENO'
    assert 'device' in report['mount_comparison']['reason']
    # Different device numbers do not hide the same UNC share or its subpath.
    alias = replace(alias, device=(0, 81), source='//synology/anime/Season')
    assert smb_probe._compare_production_mount(Path('/probe/empty'), alias, production)['status'] == 'FAIL'
    # Component boundaries keep an independent share on the same server usable.
    independent = replace(alias, source='//synology/anime-test', fs_root=PurePosixPath('/'))
    assert smb_probe._compare_production_mount(Path('/probe/empty'), independent, production)['status'] == 'PASS'
    conservative = smb_probe._compare_production_mount(
        Path('/probe/empty'), replace(independent, device=production.device), production)
    assert conservative['status'] == 'FAIL' and 'conservative' in conservative['reason']
    unknown = smb_probe._compare_production_mount(Path('/probe/empty'), independent, None)
    assert unknown['status'] == 'NEOVĚŘENO' and not unknown['comparable']
    no_reference = _parse_mounts(
        '1 1 8:1 / / rw - ext4 /dev/local rw\n'
        '11 1 0:81 / /probe rw - cifs //synology/anime-test rw\n')
    monkeypatch.setattr(smb_probe.fs, '_read_mounts', lambda: no_reference)
    assert smb_probe.probe('/probe/empty')['exit_code'] == 2
    # A bind of a production subdirectory is recognizable even when the
    # production path is an ordinary directory of the covering local mount.
    local_bind = _parse_mounts(
        '1 1 8:1 / / rw - ext4 /dev/local rw\n'
        '11 1 8:1 /mnt/nas-anime/Season /probe rw - ext4 /dev/local rw\n')
    monkeypatch.setattr(smb_probe.fs, '_read_mounts', lambda: local_bind)
    assert smb_probe.probe('/probe/empty')['exit_code'] == 2


def test_mount_report_includes_context_and_redacts_sensitive_options(tmp_path, monkeypatch):
    from app.target_execution_mounts import _parse_mounts
    with smb_probe.fs._held_evidence_fd(smb_probe.fs._open_absolute(tmp_path)) as fd:
        mount_id = smb_probe.fs._mount_id(fd)
    mounts = smb_probe.fs._read_mounts()
    actual = next(m for m in mounts if m.mount_id == mount_id)
    prefix = actual.raw.split(' - ', 1)[0].split()
    prefix[5] = 'rw,username=mount-user,password=mount-secret'
    raw = (' '.join(prefix) + ' - ' + actual.fstype +
        ' //report-user:report-secret@local/probe rw,cache=strict,vers=3.1.1,'
        'forceuid,forcegid,nativesocket,nostrictsync,closetimeo=1,acregmax=30,acdirmax=60,'
        'credentials=/private/cred,token=opaque-secret,unrecognized=hidden-value')
    work = _parse_mounts(raw)[0]
    production = _parse_mounts(
        '999999 1 0:99 / /mnt/nas-anime rw - cifs //synology/anime rw,domain=private-domain')[0]
    monkeypatch.setattr(smb_probe.fs, '_read_mounts',
        lambda: tuple(m for m in mounts if m.point not in {actual.point, production.point}) + (work, production))
    report = smb_probe.probe(tmp_path)
    assert report['exit_code'] == 0, report['checks']['work_directory']
    assert report['kernel_release'] == os.uname().release
    assert report['mount_comparison']['comparable'] is True
    assert report['mount']['mountinfo_record'].startswith(str(mount_id) + ' ')
    assert report['production_mount']['point'] == '/mnt/nas-anime'
    assert report['mount']['mount_options']
    assert 'cache=strict' in report['mount']['superblock_options']
    assert {'forceuid', 'forcegid', 'nativesocket', 'nostrictsync', 'closetimeo=1',
        'acregmax=30', 'acdirmax=60'} <= set(report['mount']['superblock_options'])
    assert 'unrecognized=[REDACTED]' in report['mount']['superblock_options']
    serialized = json.dumps(report)
    for secret in ('mount-user', 'mount-secret', 'report-user', 'report-secret',
            '/private/cred', 'opaque-secret', 'private-domain', 'hidden-value'):
        assert secret not in serialized
    assert '[REDACTED]' in serialized


def test_source_still_present_is_not_reported_as_target_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(smb_probe.fs, '_rename_noreplace', lambda *args: None)
    report = smb_probe.probe(tmp_path)
    assert report['exit_code'] == 1
    assert report['checks']['rename_missing_target']['status'] == 'PASS'
    result = report['checks']['source_absent_after_same_rename']
    assert result['status'] == 'FAIL'
    assert result['message'] == 'source name still present after rename'
    assert result['errno'] is None and result['errno_name'] is None
    assert report['checks']['reject_existing_target']['status'] == 'NEOVĚŘENO'
