#!/usr/bin/env python3
"""Create validated local SQLite snapshots; never run AnimeDB or Restic.

The CLI deliberately has no root/source/destination option or environment
override. APPDATA_ROOT is replaced only by isolated tests importing this module.
Linux dirfds, mount IDs and renameat2 are required; there is no unsafe fallback.
"""

import argparse
from contextlib import closing
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import quote
import uuid


APPDATA_ROOT = Path("/srv/nexus/appdata/anime-db")
DEFAULT_TIMEOUT = 120.0
# Standalone deployment contract; the regression suite checks these against
# app/migrations.py and the real models without importing application startup.
EXPECTED_SCHEMA_VERSION = 9
EXPECTED_TABLES = frozenset({
    "videos", "catalog_collections", "catalog_titles", "physical_naming_choices",
    "physical_layout_choices", "video_variant_groups", "manual_split_rule_videos",
    "collection_grouping_decisions", "grouping_decision_titles", "external_title_links",
    "title_metadata", "metadata_candidates", "artwork", "audio_tracks", "internal_subtitles",
    "external_subtitles", "external_subtitle_compatibilities", "unresolved_external_subtitles",
})
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_LOCAL_FILESYSTEMS = frozenset({"ext4", "xfs", "btrfs", "tmpfs", "overlay"})
_GENERATION = re.compile(r"\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{32}\Z")


class BackupError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _generation_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + uuid.uuid4().hex


def _identity(info):
    return info.st_dev, info.st_ino


def _mount_id(fd):
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as handle:
        for line in handle:
            if line.startswith("mnt_id:"):
                return int(line.split()[1])
    raise BackupError("UNSAFE_PATH", "Cannot establish filesystem mount identity")


def _mount_type(fd):
    mount_id = _mount_id(fd)
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            before, separator, after = line.partition(" - ")
            if separator and int(before.split()[0]) == mount_id:
                return after.split()[0]
    raise BackupError("UNSAFE_PATH", "Cannot establish local filesystem type")


def _protected_directory(fd):
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise BackupError("UNSAFE_PATH", "Backup directories require service ownership and no group/other writes")
    if not info.st_mode & stat.S_IWUSR:
        raise BackupError("PERMISSION", "Backup directory is not owner-writable")


class _Directories:
    """Pin directory identities instead of following replaceable path strings."""

    def __init__(self):
        self.fds = []
        self.bindings = []
        self.mount_id = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for fd in reversed(self.fds):
            os.close(fd)

    def _remember(self, fd):
        self.fds.append(fd)
        return fd

    def root(self):
        root = APPDATA_ROOT
        if not root.is_absolute() or ".." in root.parts:
            raise BackupError("UNSAFE_PATH", "Appdata root must be an absolute canonical path")
        current = self._remember(os.open("/", _DIRECTORY))
        for name in root.parts[1:]:
            current = self.child(current, name)
        _protected_directory(current)
        if _mount_type(current) not in _LOCAL_FILESYSTEMS:
            raise BackupError("UNSAFE_PATH", "Snapshots require a supported local filesystem, never NAS/autofs")
        self.mount_id = _mount_id(current)
        return current

    def child(self, parent, name, *, create=False, protected=False):
        if name in ("", ".", "..") or "/" in name:
            raise BackupError("UNSAFE_PATH", "Invalid directory component")
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise BackupError("UNSAFE_PATH", "Directory symlink or non-directory rejected")
        fd = self._remember(os.open(name, _DIRECTORY, dir_fd=parent))
        if _identity(info) != _identity(os.fstat(fd)):
            raise BackupError("UNSAFE_PATH", "Directory changed while opening")
        self.bindings.append((parent, name, fd))
        if self.mount_id is not None and _mount_id(fd) != self.mount_id:
            raise BackupError("UNSAFE_PATH", "Nested mounts and bind aliases are not allowed")
        if protected:
            _protected_directory(fd)
        return fd

    def check(self):
        for parent, name, fd in self.bindings:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or _identity(info) != _identity(os.fstat(fd)):
                raise BackupError("UNSAFE_PATH", "Directory binding changed during backup")


def _regular(fd, *, owner=False):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError("UNSAFE_PATH", "Only independent regular files are allowed")
    if owner and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise BackupError("UNSAFE_PATH", "Backup files must be private to the service account")
    if not info.st_mode & 0o444:
        raise BackupError("PERMISSION", "Source file has no read permission")
    return info


def _check_source(directory, source_fd, keep_sidecars=None):
    held = _regular(source_fd)
    if _mount_id(source_fd) != _mount_id(directory):
        raise BackupError("UNSAFE_PATH", "Source file is a mount alias")
    current = os.stat("anime.db", dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or _identity(current) != _identity(held):
        raise BackupError("UNSAFE_PATH", "Source DB identity changed")
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            fd = os.open("anime.db" + suffix, _READ_FILE, dir_fd=directory)
        except FileNotFoundError:
            continue
        try:
            _regular(fd)
            if _mount_id(fd) != _mount_id(directory):
                raise BackupError("UNSAFE_PATH", "Source sidecar is a mount alias")
            if keep_sidecars is not None:
                keep_sidecars.append(fd)
                fd = None
        finally:
            if fd is not None:
                os.close(fd)
    return held


def _uri(directory, *, readonly):
    return f"file:{quote('/proc/self/fd/' + str(directory) + '/anime.db', safe='/')}?mode={'ro' if readonly else 'rw'}"


def _sqlite_copy(source_uri, destination_uri):
    with closing(sqlite3.connect(source_uri, uri=True, timeout=5)) as source:
        source.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(destination_uri, uri=True, timeout=5)) as destination:
            source.backup(destination, pages=256, sleep=0.05)
            if destination.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise BackupError("VALIDATION", "Cannot normalize destination journal mode")


def _confine_copy(work_fd, source_fds):
    """Allow writes only in work, reads only of work and pinned source inodes.

    Landlock is irreversible. Run this in a fresh interpreter with no inherited
    SQLite connections/mappings or unrelated writable descriptors. Refuse older
    kernels instead of silently dropping write/truncate or read protections.
    """
    if os.uname().machine not in ("x86_64", "aarch64"):
        raise BackupError("SANDBOX_UNAVAILABLE", "Landlock syscall mapping is unverified on this architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(444, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
    if abi < 3:
        raise BackupError("SANDBOX_UNAVAILABLE", "SQLite copy requires enabled Landlock ABI >= 3")
    # ABI 3 filesystem rights, excluding EXECUTE and READ_DIR. Directory
    # metadata traversal remains available; actual file reads are inode-bound.
    handled = ((1 << 15) - 1) & ~((1 << 0) | (1 << 3))
    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
    ruleset = Ruleset(handled)
    policy = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if policy < 0:
        raise BackupError("SANDBOX_UNAVAILABLE", "Cannot create Landlock ruleset")
    try:
        for fd, access in [(work_fd, handled), *((fd, 1 << 2) for fd in source_fds)]:
            rule = PathRule(access, fd)
            if libc.syscall(445, policy, 1, ctypes.byref(rule), 0) != 0:
                raise BackupError("SANDBOX_UNAVAILABLE", "Cannot bind Landlock filesystem rule")
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, policy, 0) != 0:
            raise BackupError("SANDBOX_UNAVAILABLE", "Cannot enforce Landlock confinement")
    finally:
        os.close(policy)
    return abi


def _copy_worker(parent_pid, source_dir, work_fd, source_fds):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        os._exit(1)
    try:
        abi = _confine_copy(work_fd, source_fds)
        _sqlite_copy(_uri(source_dir, readonly=True), _uri(work_fd, readonly=False))
        result = {"success": True, "landlock_abi": abi}
    except Exception as exc:
        result = {"success": False, "code": _error_code(exc), "detail": str(exc)}
    print(json.dumps(result), flush=True)
    raise SystemExit(0 if result["success"] else 1)


def _copy_database(source_dir, work_fd, source_fd, sidecar_fds):
    # Exec discards inherited SQLite VFS caches and writable SHM mappings.
    # Only directory capabilities, read-only source descriptors and IPC survive.
    code = (
        "import runpy,sys; m=runpy.run_path(sys.argv[1]); "
        "m['_copy_worker'](int(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4]),"
        "[int(x) for x in sys.argv[5:]])"
    )
    source_fds = [source_fd, *sidecar_fds]
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(Path(__file__).absolute()),
         str(os.getpid()), str(source_dir), str(work_fd), *(str(fd) for fd in source_fds)],
        pass_fds=(source_dir, work_fd, *source_fds), close_fds=True,
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    try:
        result = json.loads(completed.stdout)
    except (ValueError, UnicodeError):
        raise BackupError("WORKER_FAILED", "Confined SQLite copier did not return a completed report") from None
    if completed.returncode != 0 or not result.get("success"):
        raise BackupError(result.get("code", "WORKER_FAILED"), result.get("detail", "Confined SQLite copy failed"))
    return result["landlock_abi"]


def _validate_copy(work_fd):
    if set(os.listdir(work_fd)) != {"anime.db"}:
        raise BackupError("VALIDATION", "Destination requires sidecars or contains unexpected files")
    with closing(sqlite3.connect(_uri(work_fd, readonly=True), uri=True)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ))
        if version != EXPECTED_SCHEMA_VERSION or not EXPECTED_TABLES.issubset(tables):
            raise BackupError("SCHEMA", "Unexpected schema marker or missing application tables")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("VALIDATION", "SQLite integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise BackupError("VALIDATION", "SQLite foreign_key_check failed")
        if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
            raise BackupError("VALIDATION", "Destination is not a standalone rollback-journal DB")
    if set(os.listdir(work_fd)) != {"anime.db"}:
        raise BackupError("VALIDATION", "Validation left a destination sidecar")
    return version, {"integrity_check": "ok", "foreign_key_check": [], "standalone": True, "tables": tables}


def _digest(directory, name):
    fd = os.open(name, _READ_FILE, dir_fd=directory)
    with os.fdopen(fd, "rb") as handle:
        info = _regular(handle.fileno(), owner=True)
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return digest, info.st_size


def _write_file(directory, name, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, 0o600, dir_fd=directory)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_metadata(work_fd, metadata):
    _write_file(work_fd, "snapshot.json", (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode())
    sums = "".join(f"{_digest(work_fd, name)[0]}  {name}\n" for name in ("anime.db", "snapshot.json"))
    _write_file(work_fd, "SHA256SUMS", sums.encode("ascii"))


def _verify_bundle(work_fd, metadata):
    if set(os.listdir(work_fd)) != {"anime.db", "snapshot.json", "SHA256SUMS"}:
        raise BackupError("VALIDATION", "Snapshot bundle is incomplete")
    fd = os.open("snapshot.json", _READ_FILE, dir_fd=work_fd)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        _regular(handle.fileno(), owner=True)
        if json.load(handle) != metadata:
            raise BackupError("VALIDATION", "Snapshot metadata changed")
    fd = os.open("SHA256SUMS", _READ_FILE, dir_fd=work_fd)
    with os.fdopen(fd, "r", encoding="ascii") as handle:
        _regular(handle.fileno(), owner=True)
        expected = "".join(f"{_digest(work_fd, name)[0]}  {name}\n" for name in ("anime.db", "snapshot.json"))
        if handle.read() != expected:
            raise BackupError("VALIDATION", "Snapshot checksum verification failed")
    if _digest(work_fd, "anime.db") != (metadata["database_sha256"], metadata["database_size"]):
        raise BackupError("VALIDATION", "Database changed after validation")
    for name in ("anime.db", "snapshot.json", "SHA256SUMS"):
        fd = os.open(name, _READ_FILE, dir_fd=work_fd)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    os.fsync(work_fd)


def _rename_noreplace(source_fd, source, target_fd, target):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _error_code(exc):
    if isinstance(exc, BackupError):
        return exc.code
    if isinstance(exc, sqlite3.Error):
        return "SQLITE"
    if isinstance(exc, PermissionError):
        return "PERMISSION"
    if isinstance(exc, FileNotFoundError):
        return "MISSING_SOURCE"
    if isinstance(exc, FileExistsError):
        return "COLLISION"
    if isinstance(exc, OSError) and exc.errno in (errno.ELOOP, errno.ENOTDIR):
        return "UNSAFE_PATH"
    return "IO_ERROR"


def _stop(instance, code, detail):
    return {"instance": instance, "status": "STOP", "code": code, "detail": detail,
            "remote_backup_verified": False}


def _send(fd, value):
    data = (json.dumps(value) + "\n").encode()
    while data:
        written = os.write(fd, data)
        data = data[written:]


def _worker(instance, generation, pipe, parent_pid):
    # Do not survive a killed supervisor and later publish an orphaned result.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        os._exit(1)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    started = _utc_now()
    try:
        with _Directories() as paths:
            root = paths.root()
            source_dir = paths.child(paths.child(root, instance), "db")
            source_fd = paths._remember(os.open("anime.db", _READ_FILE, dir_fd=source_dir))
            sidecar_fds = []
            try:
                source_info = _check_source(source_dir, source_fd, sidecar_fds)
            finally:
                paths.fds.extend(sidecar_fds)
            backups = paths.child(root, "backups", create=True, protected=True)
            lock = paths._remember(os.open(
                ".sqlite-backup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600, dir_fd=backups,
            ))
            _regular(lock, owner=True)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BackupError("LOCKED", "Another SQLite snapshot helper is running") from None
            partial = paths.child(backups, ".partial", create=True, protected=True)
            destination = paths.child(backups, instance, create=True, protected=True)
            work_name = instance + "-" + generation + "-" + uuid.uuid4().hex
            os.mkdir(work_name, 0o700, dir_fd=partial)
            work = paths.child(partial, work_name, protected=True)
            _send(pipe, {"event": "prepared", "work_name": work_name,
                         "work_identity": list(_identity(os.fstat(work)))})
            _write_file(work, "anime.db", b"")
            landlock_abi = _copy_database(source_dir, work, source_fd, sidecar_fds)
            _check_source(source_dir, source_fd)
            version, validation = _validate_copy(work)
            digest, size = _digest(work, "anime.db")
            metadata = {
                "format_version": 1, "instance": instance, "generation": generation,
                "source": {"path": str(APPDATA_ROOT / instance / "db/anime.db"),
                           "device": source_info.st_dev, "inode": source_info.st_ino},
                "started_at": started, "completed_at": _utc_now(),
                "git_revision": None, "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "sqlite_version": sqlite3.sqlite_version, "schema_version": version,
                "database_size": size, "database_sha256": digest, "validation": validation,
                "remote_backup_verified": False, "copy_confinement": {"landlock_abi": landlock_abi},
            }
            _write_metadata(work, metadata)
            _verify_bundle(work, metadata)
            paths.check()
            _check_source(source_dir, source_fd)
            os.fsync(partial)
            _rename_noreplace(partial, work_name, destination, generation)
            os.fsync(destination)
            os.fsync(partial)
        result = {"instance": instance, "status": "SUCCESS", "generation": generation,
                  "snapshot": str(APPDATA_ROOT / "backups" / instance / generation),
                  "remote_backup_verified": False}
    except Exception as exc:
        result = _stop(instance, _error_code(exc), str(exc))
    _send(pipe, {"event": "result", "result": result})
    os.close(pipe)
    os._exit(0)


def _quarantine(instance, generation, prepared):
    """Withdraw only this child's directory, never a colliding older snapshot."""
    with _Directories() as paths:
        root = paths.root()
        backups = paths.child(root, "backups", protected=True)
        partial = paths.child(backups, ".partial", protected=True)
        destination = paths.child(backups, instance, protected=True)
        try:
            info = os.stat(generation, dir_fd=destination, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode) or list(_identity(info)) != prepared["work_identity"]:
            return
        paths.check()
        _rename_noreplace(destination, generation, partial, "failed-" + uuid.uuid4().hex)
        os.fsync(destination)
        os.fsync(partial)


def _run_instance(instance, timeout):
    deadline = time.monotonic() + timeout
    generation = _generation_id()
    if not _GENERATION.fullmatch(generation):
        return _stop(instance, "UNSAFE_PATH", "Invalid generated snapshot name")
    # Defer termination until all resources have identifiable owners. In the
    # worker SIGKILL/parent-death remain effective even while these are blocked.
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    read_fd = write_fd = pid = None
    output = bytearray()
    status = None
    reaped = False
    failure = None
    try:
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        parent_pid = os.getpid()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            _worker(instance, generation, write_fd, parent_pid)
            os._exit(1)
        os.close(write_fd)
        write_fd = None
        os.set_blocking(read_fd, False)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        while True:
            try:
                output.extend(os.read(read_fd, 65536))
            except BlockingIOError:
                pass
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited:
                status, reaped = child_status, True
            if time.monotonic() >= deadline:
                failure = _stop(instance, "TIMEOUT", "SQLite snapshot exceeded its wall-clock deadline")
                break
            if reaped:
                break
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        failure = _stop(instance, "INTERRUPTED", "SQLite snapshot was interrupted")
    except Exception as exc:
        failure = _stop(instance, "WORKER_FAILED", str(exc))
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        if write_fd is not None:
            os.close(write_fd)
        if pid is not None and not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                _, status = os.waitpid(pid, 0)
            except ChildProcessError:
                # A signal may arrive after waitpid reaped the child but before
                # Python stored its return value. This is never success evidence.
                status = None
        if read_fd is not None:
            try:
                while chunk := os.read(read_fd, 65536):
                    output.extend(chunk)
            finally:
                os.close(read_fd)
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        except KeyboardInterrupt:
            failure = _stop(instance, "INTERRUPTED", "SQLite snapshot was interrupted")
    try:
        messages = [json.loads(line) for line in output.splitlines()]
    except (ValueError, UnicodeError):
        messages = []
        failure = failure or _stop(instance, "WORKER_FAILED", "Incomplete worker report")
    prepared = next((m for m in messages if m.get("event") == "prepared"), None)
    result = next((m["result"] for m in messages if m.get("event") == "result"), None)
    if failure is None and (status != 0 or result is None):
        failure = _stop(instance, "WORKER_FAILED", "Snapshot worker exited without a completed result")
    result = failure or result
    if result["status"] != "SUCCESS" and prepared is not None:
        try:
            _quarantine(instance, generation, prepared)
        except Exception as exc:
            result["publication_uncertain"] = True
            result["quarantine_error"] = str(exc)
    return result


def backup_instances(instances, *, timeout=DEFAULT_TIMEOUT):
    instances = list(instances)
    if not instances or len(set(instances)) != len(instances) or any(i not in ("A", "B") for i in instances):
        raise ValueError("Provide an explicit nonempty list of distinct instances A and/or B")
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Timeout must be a finite positive number of seconds")
    results = []
    interrupted = False
    for instance in instances:
        if interrupted:
            result = _stop(instance, "INTERRUPTED", "Not started after interruption of this run")
        else:
            try:
                result = _run_instance(instance, timeout)
            except KeyboardInterrupt:
                result = _stop(instance, "INTERRUPTED", "SQLite snapshot was interrupted before completion")
        results.append(result)
        interrupted = interrupted or result.get("code") == "INTERRUPTED"
    return {"format_version": 1, "expected_instances": instances, "results": results,
            "success": all(r["status"] == "SUCCESS" for r in results), "remote_backup_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances", nargs="+", choices=("A", "B"), required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS")
    args = parser.parse_args(argv)
    previous = signal.getsignal(signal.SIGTERM)
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    try:
        try:
            report = backup_instances(args.instances, timeout=args.timeout)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(report, sort_keys=True))
        if any(r.get("code") == "INTERRUPTED" for r in report["results"]):
            return 130
        return 0 if report["success"] else 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
