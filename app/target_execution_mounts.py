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


def _check_mounts(root: Path, fd: int, mounts: tuple[MountRecord, ...]) -> MountRecord:
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
