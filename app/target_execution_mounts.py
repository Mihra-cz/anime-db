"""Read-only Linux mount checks for E2A scratch trees, never production approval."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re


@dataclass(frozen=True)
class MountRecord:
    mount_id: int
    parent_id: int
    device: tuple[int, int]
    fs_root: PurePosixPath
    point: Path
    fstype: str
    source: str
    options: tuple[str, ...]
    raw: str


def _unescape(value: str) -> str:
    result = re.sub(r'\\(040|011|012|134)', lambda m: chr(int(m[1], 8)), value)
    # A literal backslash decoded from 134 is legal; reject unknown raw escapes.
    if re.search(r'\\(?!040|011|012|134)', value):
        raise ValueError('unknown mountinfo escape')
    return result


def _parse_mounts(raw: str) -> tuple[MountRecord, ...]:
    records = []
    for line in raw.splitlines():
        before, separator, after = line.partition(' - ')
        fields, tail = before.split(), after.split()
        if not separator or len(fields) < 6 or len(tail) != 3:
            raise ValueError('unverifiable mountinfo record')
        major, minor = fields[2].split(':')
        root, point = _unescape(fields[3]), _unescape(fields[4])
        if (not root.startswith('/') or not point.startswith('/')
                or '..' in PurePosixPath(root).parts or '..' in Path(point).parts):
            raise ValueError('unsafe mountinfo path')
        records.append(MountRecord(int(fields[0]), int(fields[1]), (int(major), int(minor)),
            PurePosixPath(root), Path(point), tail[0], _unescape(tail[1]),
            tuple(fields[5].split(',')), line))
    if not records or len({m.mount_id for m in records}) != len(records):
        raise ValueError('missing or duplicate mount identities')
    return tuple(records)


def _read_mounts() -> tuple[MountRecord, ...]:
    return _parse_mounts(Path('/proc/self/mountinfo').read_text(encoding='utf-8'))


def _mount_id(fd: int) -> int:
    values = [line.split(':', 1)[1].strip()
        for line in Path(f'/proc/self/fdinfo/{fd}').read_text(encoding='ascii').splitlines()
        if line.startswith('mnt_id:')]
    if len(values) != 1 or not values[0].isdigit():
        raise ValueError('descriptor mount identity unavailable')
    return int(values[0])


def _check_mounts(root: Path, fd: int, mounts: tuple[MountRecord, ...], *, allow_root_mount=False) -> MountRecord:
    """Reject subtree mounts and differently named aliases in this namespace.

    Device equality alone misses same-device bind mounts. Compare the filesystem
    root offsets of every mount, including aliases of any execution subdirectory.
    The sandbox may expose /tmp at /tmp twice; identical path projection is not
    a differently named alias. Hidden/other-namespace aliases remain unproven.
    """
    mount_id = _mount_id(fd)
    matching = [m for m in mounts if m.mount_id == mount_id]
    if len(matching) != 1:
        raise ValueError('descriptor mount missing from mountinfo')
    current = matching[0]
    info = os.fstat(fd)
    if (current.device != (os.major(info.st_dev), os.minor(info.st_dev))
            or not root.is_relative_to(current.point)):
        raise ValueError('mount and descriptor binding disagree')
    # This is a SCRATCH allowlist. No filesystem has production authorization.
    # Overlay is useful for isolated container tests only, never NAS evidence.
    if current.fstype not in {'ext4', 'btrfs', 'overlay'}:
        raise ValueError('filesystem not allowed for durable scratch rename')
    if 'rw' not in current.options or os.fstatvfs(fd).f_flag & os.ST_RDONLY:
        raise ValueError('execution mount is read-only')
    storage_root = current.fs_root / root.relative_to(current.point).as_posix()
    for other in mounts:
        if allow_root_mount and other.mount_id == mount_id:
            continue
        if other.point.is_relative_to(root):
            raise ValueError('mount at or inside execution root is forbidden')
        if other.mount_id == mount_id or other.device != current.device:
            continue
        if storage_root.is_relative_to(other.fs_root):
            alias = other.point / storage_root.relative_to(other.fs_root)
            if alias != root:
                raise ValueError('bind mount aliases execution root or ancestor')
        elif other.fs_root.is_relative_to(storage_root):
            raise ValueError('bind mount aliases execution subtree')
    return current


def _validate_share(share: str, share_root: str) -> None:
    parts = share.split('/') if type(share) is str else ()
    if (len(parts) != 4 or parts[:2] != ['', ''] or not all(parts[2:])
            or any(c in share for c in ('\\', '\x00', '@', '?', '#'))
            or any(p in {'.', '..'} for p in parts[2:])
            or type(share_root) is not str or not share_root.startswith('/')
            or str(PurePosixPath(share_root)) != share_root or '..' in PurePosixPath(share_root).parts):
        raise ValueError('explicit canonical SMB server/share and filesystem subtree required')


def _check_production_mount(root: Path, fd: int | None, mounts: tuple[MountRecord, ...], *,
                            share: str, share_root: str, writable: bool) -> MountRecord:
    """Explicit media policy; never extends the local scratch/state allowlist.

    The fd-less check binds ordinary RO admission using local mountinfo only.
    Live execution additionally pins descriptors, device and mount identity.
    """
    _validate_share(share, share_root)
    root = Path(root)
    covering = [m for m in mounts if root.is_relative_to(m.point)]
    if not covering:
        raise ValueError('production media mount unavailable')
    depth = max(len(m.point.parts) for m in covering)
    visible = [m for m in covering if len(m.point.parts) == depth]
    if len(visible) != 1:
        raise ValueError('ambiguous production media mount')
    current = visible[0]
    by_id = {m.mount_id: m for m in mounts}
    ancestors, cursor = set(), current
    while cursor is not None:
        if cursor.mount_id in ancestors:
            raise ValueError('cyclic production mount ancestry')
        ancestors.add(cursor.mount_id)
        parent = by_id.get(cursor.parent_id)
        if parent is cursor and cursor.point == Path('/'):
            break
        if parent is not None and parent.point != cursor.point and parent.point not in cursor.point.parents:
            raise ValueError('production mount parent binding differs')
        cursor = parent
    if any(m.point in current.point.parents and m.mount_id not in ancestors for m in mounts):
        raise ValueError('production library is hidden by an ancestor mount')
    if (current.fstype != 'cifs' or current.source.casefold() != share.casefold()
            or str(current.fs_root / root.relative_to(current.point).as_posix()) != share_root):
        raise ValueError('production media server/share/subtree differs')
    if fd is not None:
        info = os.fstat(fd)
        if (_mount_id(fd) != current.mount_id
                or current.device != (os.major(info.st_dev), os.minor(info.st_dev))):
            raise ValueError('production descriptor mount/device differs')
    if writable and ('rw' not in current.options or (fd is not None
            and os.fstatvfs(fd).f_flag & os.ST_RDONLY)):
        raise ValueError('production execution requires a writable media mount')
    for other in mounts:
        if other.mount_id == current.mount_id:
            continue
        if other.point.is_relative_to(root):
            raise ValueError('mount at or inside production library is forbidden')
        same_share = other.fstype == 'cifs' and other.source.casefold() == current.source.casefold()
        if not same_share and other.device != current.device:
            continue
        storage = PurePosixPath(share_root)
        if storage.is_relative_to(other.fs_root):
            if other.point / storage.relative_to(other.fs_root) != root:
                raise ValueError('production library has a visible mount alias')
        elif other.fs_root.is_relative_to(storage):
            raise ValueError('production library subtree has a visible mount alias')
    return current
