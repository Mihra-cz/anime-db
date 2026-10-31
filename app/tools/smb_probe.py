"""Opt-in filesystem diagnostics in an existing empty directory, never execution.

No mount, database, scanner, journal, retry, copy fallback or cleanup is used.
SMB runs require separate user approval. Artifacts are retained for inspection.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .. import target_execution_filesystem as fs


_CHECKS = (
    'work_directory', 'prepare_test_files', 'same_mount',
    'fsync_root_directory', 'fsync_created_file', 'fsync_collision_file',
    'fsync_created_source_directory', 'fsync_created_target_directory', 'rename_missing_target',
    'source_absent_after_same_rename',
    'identity_after_same_directory_rename', 'fsync_file_after_same_directory_rename',
    'fsync_source_after_same_directory_rename', 'reject_existing_target',
    'collision_contents_preserved', 'move_between_directories', 'source_absent_after_cross_rename',
    'identity_after_cross_directory_rename', 'content_preserved',
    'fsync_file_after_cross_directory_rename', 'fsync_destination_directory',
    'fsync_source_directory', 'close_descriptors', 'power_loss_durability',
)
_STAT_FIELDS = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
    'st_size', 'st_atime_ns', 'st_mtime_ns', 'st_ctime_ns')
_STABLE_FIELDS = tuple(name for name in _STAT_FIELDS if name not in {'st_atime_ns', 'st_ctime_ns'})


_PRODUCTION_ROOT = Path('/mnt/nas-anime')
_SMB_TYPES = {'cifs', 'smb3'}
_LOCAL_TYPES = {'ext4', 'btrfs', 'overlay'}
# Public metadata only: unknown option values/flags are redacted by default.
_PUBLIC_FLAGS = set(('rw ro nosuid nodev noexec relatime noatime strictatime lazytime '
    'sync async dirsync acl noacl user_xattr nouser_xattr serverino noserverino '
    'perm noperm unix nounix posixpaths noposixpaths mapchars mapposix hard soft '
    'intr nointr seal sign nosharesock persistenthandles resilienthandles '
    'mfsymlinks dynperm nobrl nohandlecache forceuid forcegid nativesocket nostrictsync').split())
_PUBLIC_VALUES = set(('vers cache uid gid file_mode dir_mode actimeo rsize wsize '
    'bsize sec echo_interval errors closetimeo acregmax acdirmax').split())


def _redact_options(options):
    return [option if option in _PUBLIC_FLAGS or
        ('=' in option and option.split('=', 1)[0] in _PUBLIC_VALUES)
        else option.split('=', 1)[0] + '=[REDACTED]' if '=' in option
        else '[REDACTED]' for option in options]


def _redact_source(source):
    if any(char in source for char in ('=', '?', '#')):
        return '[REDACTED]'
    if source.startswith('//'):
        return re.sub(r'(?<=//)[^/]*@', '[REDACTED]@', source)
    return '[REDACTED]' if '@' in source else source


def _mount_report(mount):
    if mount is None:
        return None
    before, _, after = mount.raw.partition(' - ')
    fields, tail = before.split(), after.split()
    fields[5] = ','.join(_redact_options(fields[5].split(',')))
    fields[6:] = [value if re.fullmatch(r'(shared|master|propagate_from):[0-9]+|unbindable', value)
        else '[REDACTED]' for value in fields[6:]]
    tail[1] = _redact_source(tail[1])
    super_options = _redact_options(tail[2].split(','))
    tail[2] = ','.join(super_options)
    sanitized = ' '.join(fields) + ' - ' + ' '.join(tail)
    return dict(mount_id=mount.mount_id, device=list(mount.device), fstype=mount.fstype,
        source=_redact_source(mount.source), filesystem_root=str(mount.fs_root),
        point=str(mount.point), mount_options=_redact_options(mount.options),
        superblock_options=super_options, mountinfo_record=sanitized,
        redacted=sanitized != mount.raw)


def _covering_mount(path, mounts):
    candidates = [m for m in mounts if path.is_relative_to(m.point)]
    if not candidates:
        raise ValueError('path has no covering mountinfo record')
    depth = max(len(m.point.parts) for m in candidates)
    top = [m for m in candidates if len(m.point.parts) == depth]
    projections = {(m.device, m.fstype, m.source, m.fs_root, m.point) for m in top}
    if len(projections) != 1:
        raise ValueError('ambiguous stacked mount identity; no writes permitted')
    return top[0]


def _unc_parts(source):
    if not source.startswith('//') or any(c in source for c in ('?', '#', '\\')):
        return None
    parts = source[2:].rstrip('/').split('/')
    if len(parts) < 2 or any(part in {'', '.', '..'} for part in parts):
        return None
    parts[0] = parts[0].rsplit('@', 1)[-1]
    if parts[0] in {'', '.', '..'}:
        return None
    return tuple(part.casefold() for part in parts)


def _compare_production_mount(path, work, production):
    result = dict(status='NEOVĚŘENO', comparable=production is not None,
        local_probe=work.fstype in _LOCAL_TYPES)
    if production is None:
        return dict(result, reason='production mount is absent from mountinfo; only a known local filesystem may be probed')
    work_path = work.fs_root / path.relative_to(work.point).as_posix()
    prod_path = production.fs_root / _PRODUCTION_ROOT.relative_to(production.point).as_posix()
    relation = ('equal' if work_path == prod_path else
        'inside_production' if work_path.is_relative_to(prod_path) else
        'contains_production' if prod_path.is_relative_to(work_path) else 'disjoint')
    if work.device != production.device and (work.fstype, work.source) != (production.fstype, production.source):
        relation = 'not_comparable'
    result.update(same_device=work.device == production.device,
        same_fstype=work.fstype == production.fstype, same_source=work.source == production.source,
        work_filesystem_path=str(work_path), production_filesystem_path=str(prod_path),
        filesystem_path_relation=relation)
    if work.device == production.device:
        return dict(result, status='FAIL', reason='matching production device; conservative refusal even if source or filesystem root differs')
    if work.fstype in _SMB_TYPES:
        ours, theirs = _unc_parts(work.source), _unc_parts(production.source)
        if production.fstype not in _SMB_TYPES or ours is None or theirs is None:
            return dict(result, comparable=False, reason='SMB share identity cannot be safely compared; no writes permitted')
        if ours[:2] == theirs[:2]:
            return dict(result, status='FAIL', reason='same production SMB share or a subpath of it')
        if ours[1] == theirs[1]:
            return dict(result, comparable=False, reason='same share name with different server names may be an alias; no writes permitted')
        return dict(result, status='PASS', reason='distinct UNC share components and different devices')
    if work.fstype not in _LOCAL_TYPES:
        return dict(result, comparable=False, reason='unrecognized work filesystem identity; no writes permitted')
    if work.fstype == production.fstype and work.source == production.source:
        return dict(result, status='FAIL', reason='same production filesystem source; conservative refusal')
    return dict(result, status='PASS', reason='distinct local filesystem and production mount identity')


def _source_absent(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise ValueError('source name still present after rename')


def _snapshot(fd):
    value = os.fstat(fd)
    return {name: getattr(value, name) for name in _STAT_FIELDS}


def _error(exc):
    code = getattr(exc, 'errno', None)
    return dict(status='FAIL', errno=code, errno_name=errno.errorcode.get(code),
        error_type=type(exc).__name__, message=str(exc))


def _digest(fd):
    # Only the small files minted by this probe may be read, with bounded reads.
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > 65536:
        raise ValueError('unexpected test file type or size')
    result, offset = hashlib.sha256(), 0
    while offset <= 65536:
        chunk = os.pread(fd, 65536, offset)
        if not chunk:
            break
        result.update(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if offset != before.st_size or any(getattr(before, k) != getattr(after, k) for k in fields):
        raise ValueError('test file changed while reading')
    return result.hexdigest()


def _create_file(parent, name, payload):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    with fs._held_evidence_fd(os.open(name, flags, 0o600, dir_fd=parent)) as fd:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError(errno.EIO, 'test file write made no progress')
            remaining = remaining[written:]


def probe(work_dir: Path | str) -> dict:
    """Write only beneath the admitted empty directory; retain all own artifacts.

    The caller must exclusively control this directory throughout the probe.
    This deliberately uses the syscall helper, never the executor's admission.
    """
    checks = {name: dict(status='NEOVĚŘENO', reason='not reached') for name in _CHECKS}
    checks['power_loss_durability'] = dict(status='NEOVĚŘENO',
        reason='Successful fsync is an acknowledgement, not proof of power-loss durability.')
    observations = {}
    report = dict(version=1, work_dir=str(work_dir), checks=checks, observations=observations,
        production_execution_authorized=False, kernel_release=os.uname().release,
        mount=None, production_mount=None,
        mount_comparison=dict(status='NEOVĚŘENO', comparable=False, reason='not reached'))
    current = 'work_directory'

    def sync(name, fd):
        try:
            os.fsync(fd)
            checks[name] = dict(status='PASS', errno=0, errno_name=None)
        except OSError as exc:
            # Continue collecting syscall results; never convert an error to PASS.
            checks[name] = _error(exc)

    def identity(name, pinned, reopened):
        changed = {field: dict(before=observations['before'][field],
            pinned=pinned[field], reopened=reopened[field]) for field in _STABLE_FIELDS
            if pinned[field] != observations['before'][field] or reopened[field] != observations['before'][field]}
        positive_inode = reopened['st_ino'] > 0
        checks[name] = dict(status='PASS' if not changed and positive_inode else 'FAIL',
            changed_fields=changed, positive_inode=positive_inode,
            informational_fields=['st_atime_ns', 'st_ctime_ns'])

    try:
        path = fs._absolute(Path(work_dir))
        forbidden = Path('/mnt/nas-anime')
        if path == Path('/') or path == forbidden or forbidden in path.parents:
            raise ValueError('production library paths and filesystem root are forbidden')
        mounts = fs._read_mounts()  # Metadata only; never open the production path.
        work_mount = _covering_mount(path, mounts)
        covering_production = _covering_mount(_PRODUCTION_ROOT, mounts)
        production_mount = (covering_production if covering_production.point == _PRODUCTION_ROOT
            or covering_production.fstype in _SMB_TYPES else None)
        if production_mount is None:
            # A bind can expose the protected path even without a dedicated
            # production mountpoint. Compare projected component paths only.
            projected_work = work_mount.fs_root / path.relative_to(work_mount.point).as_posix()
            projected_production = covering_production.fs_root / _PRODUCTION_ROOT.relative_to(covering_production.point).as_posix()
            same_filesystem = (work_mount.device == covering_production.device or
                (work_mount.fstype, work_mount.source) == (covering_production.fstype, covering_production.source))
            if same_filesystem and projected_work.is_relative_to(projected_production):
                production_mount = covering_production
        report['mount'] = _mount_report(work_mount)
        report['production_mount'] = _mount_report(production_mount)
        comparison = _compare_production_mount(path, work_mount, production_mount)
        report['mount_comparison'] = comparison
        if comparison['status'] == 'FAIL' or (comparison['status'] != 'PASS' and not comparison['local_probe']):
            raise ValueError(comparison['reason'])
        with ExitStack() as stack:
            def hold(fd):
                return stack.enter_context(fs._held_evidence_fd(fd))

            root = hold(fs._open_absolute(path))  # No symlinks in any component.
            fs._secure_directory(root)
            if os.listdir(root):
                raise ValueError('work directory must already exist and be completely empty')
            checks[current] = dict(status='PASS', empty=True)
            mount_id = fs._mount_id(root)
            mount = next((m for m in mounts if m.mount_id == mount_id), None)
            if mount is None:
                raise ValueError('work directory mount is missing from mountinfo')
            if ((mount.device, mount.fstype, mount.source, mount.fs_root, mount.point)
                    != (work_mount.device, work_mount.fstype, work_mount.source, work_mount.fs_root, work_mount.point)
                    or mount.device != (os.major(os.fstat(root).st_dev), os.minor(os.fstat(root).st_dev))
                    or fs._read_mounts() != mounts):
                raise ValueError('work mount identity changed before writes; STOP')
            report['mount'] = _mount_report(mount)
            observations['work_directory'] = _snapshot(root)

            current = 'prepare_test_files'
            os.mkdir('source', 0o700, dir_fd=root)
            os.mkdir('target', 0o700, dir_fd=root)
            source = hold(os.open('source', fs._DIRECTORY, dir_fd=root))
            target = hold(os.open('target', fs._DIRECTORY, dir_fd=root))
            current = 'same_mount'
            if any(os.fstat(fd).st_dev != os.fstat(root).st_dev or fs._mount_id(fd) != mount_id
                    for fd in (source, target)):
                raise ValueError('test directories are not on the same device and mount')
            checks[current] = dict(status='PASS', st_dev=os.fstat(root).st_dev, mount_id=mount_id)
            observations['source_directory'] = _snapshot(source)
            observations['target_directory'] = _snapshot(target)
            current = 'prepare_test_files'
            payload, collision = os.urandom(65536), b'Existing destination: must not be overwritten.\n'
            _create_file(source, 'payload.bin', payload)
            _create_file(target, 'collision.bin', collision)
            # Match the executor's read-only pinned file descriptors for fsync.
            pinned = hold(os.open('payload.bin', fs._FILE, dir_fd=source))
            collision_fd = hold(os.open('collision.bin', fs._FILE, dir_fd=target))
            observations['before'] = _snapshot(pinned)
            observations['collision_before'] = _snapshot(collision_fd)
            observations['expected_sha256'] = hashlib.sha256(payload).hexdigest()
            observations['collision_sha256'] = hashlib.sha256(collision).hexdigest()
            checks[current] = dict(status='PASS', payload_bytes=len(payload))
            sync('fsync_created_file', pinned)
            sync('fsync_collision_file', collision_fd)
            sync('fsync_created_source_directory', source)
            sync('fsync_created_target_directory', target)
            sync('fsync_root_directory', root)

            current = 'rename_missing_target'
            fs._rename_noreplace(source, 'payload.bin', source, 'renamed.bin')
            checks[current] = dict(status='PASS', errno=0)
            current = 'source_absent_after_same_rename'
            _source_absent(source, 'payload.bin')
            checks[current] = dict(status='PASS')
            current = 'identity_after_same_directory_rename'
            reopened = hold(os.open('renamed.bin', fs._FILE, dir_fd=source))
            observations['pinned_after_same'] = _snapshot(pinned)
            observations['reopened_after_same'] = _snapshot(reopened)
            identity(current, observations['pinned_after_same'], observations['reopened_after_same'])
            sync('fsync_file_after_same_directory_rename', pinned)
            sync('fsync_source_after_same_directory_rename', source)

            current = 'reject_existing_target'
            try:
                fs._rename_noreplace(source, 'renamed.bin', target, 'collision.bin')
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                checks[current] = dict(status='PASS', errno=exc.errno, errno_name='EEXIST')
            else:
                raise ValueError('RENAME_NOREPLACE unexpectedly succeeded with an existing test target; STOP')
            current = 'collision_contents_preserved'
            named_source = hold(os.open('renamed.bin', fs._FILE, dir_fd=source))
            named_collision = hold(os.open('collision.bin', fs._FILE, dir_fd=target))
            if (_digest(named_source) != observations['expected_sha256']
                    or _digest(named_collision) != observations['collision_sha256']):
                raise ValueError('source or existing target changed during rejected rename; STOP')
            checks[current] = dict(status='PASS')

            current = 'move_between_directories'
            fs._rename_noreplace(source, 'renamed.bin', target, 'moved.bin')
            checks[current] = dict(status='PASS', errno=0)
            current = 'source_absent_after_cross_rename'
            _source_absent(source, 'renamed.bin')
            checks[current] = dict(status='PASS')
            current = 'identity_after_cross_directory_rename'
            reopened_target = hold(os.open('moved.bin', fs._FILE, dir_fd=target))
            observations['pinned_after_cross'] = _snapshot(pinned)
            observations['reopened_after_cross'] = _snapshot(reopened_target)
            identity(current, observations['pinned_after_cross'], observations['reopened_after_cross'])
            # Same order as the existing primitive: file, target parent, source parent.
            sync('fsync_file_after_cross_directory_rename', pinned)
            sync('fsync_destination_directory', target)
            sync('fsync_source_directory', source)
            current = 'content_preserved'
            observations['actual_sha256'] = _digest(reopened_target)
            if (observations['actual_sha256'] != observations['expected_sha256']
                    or _digest(named_collision) != observations['collision_sha256']):
                raise ValueError('test file content was not preserved')
            checks[current] = dict(status='PASS', sha256=observations['actual_sha256'])
            current = 'close_descriptors'
        checks[current] = dict(status='PASS')
    except (OSError, ValueError) as exc:
        checks[current] = _error(exc)

    report['summary'] = {status: sum(row['status'] == status for row in checks.values())
        for status in ('PASS', 'FAIL', 'NEOVĚŘENO')}
    report['exit_code'] = (2 if checks['work_directory']['status'] != 'PASS' else
        0 if all(row['status'] == 'PASS' for name, row in checks.items()
            if name != 'power_loss_durability') else 1)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True,
        help='Existing empty caller-owned directory; SMB execution requires separate approval.')
    args = parser.parse_args(argv)
    report = probe(args.work_dir)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return report['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
