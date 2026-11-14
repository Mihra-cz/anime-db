"""Descriptor-relative host IO; no privileged publication into app-owned paths."""
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess


def open_directory(path):
    """Walk from / using openat(O_NOFOLLOW) for *every* component."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute canonical directory required')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(path):
    path = Path(path)
    fd = open_directory(path.parent)
    try:
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    finally:
        os.close(fd)


# A fresh interpreter avoids fork-with-threads and permanently drops credentials
# before executing this fixed program. The only filesystem authority passed is
# one already-open directory, never a path through a user-owned ancestor.
WRITE_PROGRAM = r'''
import ctypes, errno, os, secrets, stat, sys
fd, name, mode, exclusive = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
if name in ('', '.', '..') or '/' in name: raise ValueError('basename required')
info = os.fstat(fd)
if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
    raise ValueError('runtime directory ownership changed')
temp = '.' + name + '.' + secrets.token_hex(12)
output = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC, mode, dir_fd=fd)
try:
    os.fchmod(output, mode)
    data = sys.stdin.buffer.read()
    while data:
        data = data[os.write(output, data):]
    os.fsync(output)
    if exclusive:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
        if rename(fd, os.fsencode(temp), fd, os.fsencode(name), 1):
            code = ctypes.get_errno()
            sys.exit(17 if code == errno.EEXIST else 1)
    else:
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)
finally:
    os.close(output)
    try: os.unlink(temp, dir_fd=fd)
    except FileNotFoundError: pass
'''


class RuntimeDirectory:
    def __init__(self, path, uid, gid):
        self.path, self.uid, self.gid = Path(path), uid, gid
        self.fd = None
        self.fd = open_directory(path)
        info = os.fstat(self.fd)
        if info.st_uid != uid or info.st_mode & 0o022:
            self.close()
            raise ValueError('runtime directory requires runtime owner and no group/other writes')
        self.identity = info.st_dev, info.st_ino

    def check(self):
        fd = open_directory(self.path)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self.identity:
                raise ValueError('runtime directory replaced; stop and revalidate')
        finally:
            os.close(fd)

    def read(self, name):
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('basename required')
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.fd)

    def write(self, name, value, *, exclusive=False, mode=0o600):
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('basename required')
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
        credentials = dict(user=self.uid, group=self.gid, extra_groups=[]) if os.geteuid() == 0 else {}
        if os.geteuid() != 0 and (os.geteuid(), os.getegid()) != (self.uid, self.gid):
            raise ValueError('runtime identity differs')
        try:
            result = subprocess.run(['/usr/bin/python3', '-I', '-c', WRITE_PROGRAM,
                str(self.fd), name, str(mode), str(int(exclusive))], input=raw,
                pass_fds=(self.fd,), cwd='/', env={'PATH':'/usr/bin:/bin'},
                capture_output=True, timeout=10, **credentials)
        except subprocess.TimeoutExpired as exc:
            raise OSError(errno.ETIMEDOUT, 'non-root runtime publication timed out') from exc
        if result.returncode == errno.EEXIST:
            raise FileExistsError(errno.EEXIST, 'record already exists', name)
        if result.returncode:
            raise OSError('non-root runtime publication failed')

    def close(self):
        if getattr(self, 'fd', None) is not None:
            os.close(self.fd)
            self.fd = None

    def __del__(self):
        self.close()


def _descriptor_mount_id(fd):
    values = [line.split(':', 1)[1].strip()
        for line in Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines()
        if line.startswith('mnt_id:')]
    if len(values) != 1 or not values[0].isdigit():
        raise ValueError('bind anchor mount identity unavailable')
    return values[0]


def ensure_bindings(bindings):
    """Publish stable root-private bind aliases from descriptors, never live paths.

    No unmount on coordinator exit: app/edge use these same generation anchors.
    Reboot clears mounts; an explicit boot/start revalidates and rebuilds them.
    """
    import ctypes
    mount = ctypes.CDLL(None,use_errno=True).mount
    mount.argtypes = [ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_ulong,ctypes.c_void_p]
    mount.restype = ctypes.c_int
    for row in bindings:
        source = open_directory(row['source'])
        try:
            info=os.fstat(source)
            identity=(row['dev'],row['ino'])
            if (info.st_dev,info.st_ino)!=identity:
                raise ValueError('approved bind source identity changed')
            target=Path(row['target'])
            # Ancestors of targets are checked as root-controlled by the
            # generation resolver; openat also refuses a substituted symlink.
            parent=open_directory(target.parent)
            try:
                fcntl.flock(parent,fcntl.LOCK_EX)
                leaf=os.stat(target.name,dir_fd=parent,follow_symlinks=False)
                if not stat.S_ISDIR(leaf.st_mode):raise ValueError('unsafe bind anchor')
                if (leaf.st_dev,leaf.st_ino)==identity:continue
                leaf_fd=os.open(target.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
                try:
                    # Same-device bind mounts are invisible to ismount(). A
                    # different mount ID must never be covered by another bind.
                    if (leaf.st_uid!=0 or leaf.st_mode & 0o077
                            or _descriptor_mount_id(leaf_fd)!=_descriptor_mount_id(parent)):
                        raise ValueError('unexpected existing bind anchor')
                finally:os.close(leaf_fd)
                destination=f'/proc/self/fd/{parent}/{target.name}'
                if mount(os.fsencode(f'/proc/self/fd/{source}'),os.fsencode(destination),None,4096,None):
                    code=ctypes.get_errno()
                    raise OSError(code,os.strerror(code))
                pinned=os.stat(target.name,dir_fd=parent,follow_symlinks=False)
                if (pinned.st_dev,pinned.st_ino)!=identity:
                    raise ValueError('bind anchor identity differs')
            finally:os.close(parent)
        finally:os.close(source)
