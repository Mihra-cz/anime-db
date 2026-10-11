"""Local durable V6 journal. Records caller evidence; never executes or resumes it."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import io
import os
from pathlib import Path
import stat
import threading

from .target_execution_manifest import (
    ExecutionManifest, JournalEntry, _canonical, _digest, _strict_json_load,
    can_continue_after_failure as _failure_conditions_met,
    journal_from_json, journal_to_json, manifest_to_json,
)
from .target_planner_filesystem import validate_inventory_root
from .tools import execution_manifest as manifest_cli

_VERSION = 1
_SCHEMA = 'animedb-v6-execution-journal'
_MAX_FRAME_BYTES = 65536
_TRANSITIONS = {
    'PENDING': frozenset({'FS_APPLIED', 'FAILED'}),
    'FS_APPLIED': frozenset({'FS_VERIFIED', 'FAILED'}),
    'FS_VERIFIED': frozenset({'DB_RECONCILED', 'FAILED'}),
    'DB_RECONCILED': frozenset({'COMPLETE', 'FAILED'}),
    'COMPLETE': frozenset(),
    'FAILED': frozenset(),
}


class JournalError(ValueError):
    """Rejected journal identity, path or explicit state transition."""


class JournalBusyError(JournalError):
    """An independent session already holds the journal lock."""


class ReconciliationRequired(JournalError):
    """History is ambiguous; validated prefix is diagnostic evidence only."""

    def __init__(self, message: str, entries: tuple[JournalEntry, ...] = ()):
        super().__init__(message)
        self.entries = tuple(entries)


@dataclass(frozen=True)
class JournalSnapshot:
    manifest_id: str
    entries: tuple[JournalEntry, ...]
    reconciliation_required: bool


def _header(manifest: ExecutionManifest) -> dict:
    # Reuse the current manifest validator, including payload/hash and typed identity.
    raw = manifest_to_json(manifest)
    return {'schema': _SCHEMA, 'version': _VERSION, 'sequence': 0,
        'manifest_id': manifest.manifest_id,
        'manifest_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest()}


def _frame(body: dict) -> tuple[bytes, str]:
    digest = _digest(body)
    raw = (_canonical({**body, 'sha256': digest}) + '\n').encode('utf-8')
    if len(raw) > _MAX_FRAME_BYTES:
        raise JournalError('journal frame exceeds maximum size')
    return raw, digest


def _validate_entry(manifest: ExecutionManifest, history: list[JournalEntry],
                    completed: set[str], entry: JournalEntry) -> None:
    if type(entry) is not JournalEntry:
        raise JournalError('JournalEntry required')
    # The legacy dataclass constructor is permissive; keep it compatible and
    # enforce its existing strict serialized types at this persistence boundary.
    try:
        journal_from_json(journal_to_json(entry))
    except (TypeError, ValueError) as exc:
        raise JournalError('invalid typed journal entry') from exc
    actions = manifest.payload.actions
    if entry.manifest_id != manifest.manifest_id or entry.action_index >= len(actions):
        raise JournalError('journal manifest/action index mismatch')
    action = actions[entry.action_index]
    if entry.action_id != action.action_id:
        raise JournalError('journal action identity mismatch')
    if action.criticality == 'REVIEW':
        raise JournalError('criticality REVIEW cannot enter execution lifecycle')
    failure_values = (entry.failure_code, entry.source_intact, entry.target_absent,
        entry.primary_dependencies_clear, entry.failure_journaled)
    if entry.state != 'FAILED' and any(value is not None for value in failure_values):
        raise JournalError('failure evidence belongs only to FAILED')
    if entry.state == 'FAILED' and not entry.failure_code.strip():
        raise JournalError('nonempty failure code required')

    previous = history[-1] if history else None
    if previous is not None and entry.action_index == previous.action_index:
        if entry.state not in _TRANSITIONS[previous.state]:
            raise JournalError('invalid or duplicate journal transition')
        return
    expected_index = previous.action_index + 1 if previous else 0
    if entry.action_index != expected_index or entry.state != 'PENDING':
        raise JournalError('actions must start PENDING in immutable manifest order')
    if previous is not None and previous.state != 'COMPLETE':
        # Both callers supply validated history; the live writer adds entries
        # only after fsync. Caller booleans cannot waive recorded FS/DB progress.
        before_failure = history[-2] if len(history) >= 2 else None
        if (before_failure is None or before_failure.state != 'PENDING'
                or (before_failure.manifest_id, before_failure.action_id, before_failure.action_index)
                    != (previous.manifest_id, previous.action_id, previous.action_index)
                or not _failure_conditions_met(manifest, previous)):
            raise JournalError('STOP: prior action incomplete or failure cannot continue')
    if any(dependency not in completed for dependency in action.depends_on):
        raise JournalError('STOP: action dependencies are not explicitly COMPLETE')


def _open_parent(path: Path, manifest: ExecutionManifest, library_roots: tuple[Path, ...],
                 forbidden_files: tuple[Path, ...]) -> int:
    if not path.is_absolute() or '..' in path.parts:
        raise JournalError('journal requires an absolute path without parent traversal')
    validate_inventory_root(path)
    manifest_cli.validate_local_output(path, Path(manifest.payload.library_root), *library_roots,
        forbidden_files=forbidden_files)
    if manifest_cli._mount_fstype(path.parent) in {'tmpfs', 'ramfs'}:
        raise JournalError('volatile memory filesystem cannot hold durable journal')
    # Hold directory descriptors instead of reopening a checked pathname for writes.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        ancestors = {_identity(os.fstat(fd))}
        for part in path.parent.parts[1:]:
            previous = fd
            fd = os.open(part, flags, dir_fd=previous)
            os.close(previous)
            ancestors.add(_identity(os.fstat(fd)))
        # Also reject a differently named directory alias (for example a bind
        # alias) of the repository/library, without traversing their contents.
        for root in (Path(__file__).resolve().parents[1],
                Path(manifest.payload.library_root), *library_roots):
            try:
                if _identity(root.stat()) in ancestors:
                    raise JournalError('journal directory aliases repository/library root')
            except FileNotFoundError:
                pass
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise JournalError('journal parent must be owned by writer and not group/world writable')
        return fd
    except BaseException as exc:
        _close_after_failure(fd, exc)
        raise


def _lock(fd: int, exclusive: bool) -> None:
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise JournalBusyError('journal held by another session') from exc


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _close_after_failure(fd: int, primary: BaseException) -> None:
    try:
        os.close(fd)
    except BaseException as exc:
        # A failed close may already have released fd; preserve cause, no retry.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not isinstance(primary, (KeyboardInterrupt, SystemExit)):
            exc.add_note(f'journal STOP: prior failure: {type(primary).__name__}: {primary}')
            for note in tuple(getattr(primary, '__notes__', ())):
                exc.add_note(note)
            raise exc from primary
        notes = tuple(getattr(exc, '__notes__', ()))
        primary.add_note(f'journal STOP: cleanup failed: {type(exc).__name__}: {exc}')
        for note in notes:
            primary.add_note(note)


def _close_fds(fds: tuple[int | None, ...], primary: BaseException | None = None) -> None:
    selected = primary
    for fd in fds:
        if fd is None:
            continue
        try:
            if selected is None:
                os.close(fd)
            else:
                _close_after_failure(fd, selected)
        except BaseException as exc:
            selected = exc
    if selected is not primary:
        raise selected


@contextmanager
def _history_reader(fd: int):
    # FileIO owns copies returned by its opener, including constructor failure.
    # The label is not reopened: only the held journal descriptor is duplicated.
    stream = io.open('<journal-history>', 'rb', opener=lambda _name, _flags: os.dup(fd))
    primary = None
    try:
        yield stream
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            stream.close()
        except BaseException as exc:
            if primary is None:
                raise
            if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not isinstance(primary, (KeyboardInterrupt, SystemExit)):
                raise exc from primary
            notes = tuple(getattr(exc, '__notes__', ()))
            primary.add_note(f'journal STOP: reader cleanup failed: {type(exc).__name__}: {exc}')
            for note in notes:
                primary.add_note(note)


def _check_parent(path: Path, parent_fd: int) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            previous = fd
            fd = os.open(part, flags, dir_fd=previous)
            os.close(previous)
        info = os.fstat(fd)
        if (_identity(info) != _identity(os.fstat(parent_fd)) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise JournalError('journal parent changed or became unsafe')
    except BaseException as exc:
        _close_after_failure(fd, exc)
        raise
    else:
        os.close(fd)


def _check_file(fd: int, parent_fd: int, name: str) -> os.stat_result:
    info = os.fstat(fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or info.st_mode & 0o022
            or not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(info)):
        raise JournalError('journal must remain an independent regular file at its original path')
    return info


def _write_all(fd: int, raw: bytes) -> None:
    remaining = memoryview(raw)
    while remaining:
        try:
            count = os.write(fd, remaining)
        except InterruptedError:
            continue
        if count <= 0:
            raise OSError('journal write made no progress')
        remaining = remaining[count:]


def _read_history(fd: int, manifest: ExecutionManifest) -> tuple[JournalEntry, ...]:
    expected_header = _header(manifest)
    entries: list[JournalEntry] = []
    completed: set[str] = set()
    previous_digest = None
    # The duplicate descriptor preserves the held lock; this reader never writes.
    with _history_reader(fd) as stream:
        stream.seek(0)
        sequence = 0
        while True:
            offset = stream.tell()
            raw = stream.readline(_MAX_FRAME_BYTES + 1)
            if not raw:
                break
            try:
                if len(raw) > _MAX_FRAME_BYTES or not raw.endswith(b'\n'):
                    raise JournalError('incomplete or oversized frame')
                value = _strict_json_load(raw.decode('utf-8'))
                if type(value) is not dict:
                    raise JournalError('journal frame must be an object')
                body = {k: v for k, v in value.items() if k != 'sha256'}
                canonical, digest = _frame(body)
                if raw != canonical or value.get('sha256') != digest:
                    raise JournalError('noncanonical frame or checksum mismatch')
                if type(body.get('sequence')) is not int or body['sequence'] != sequence:
                    raise JournalError('noncontiguous journal sequence')
                if sequence == 0:
                    if _canonical(body) != _canonical(expected_header):
                        raise JournalError('journal version/manifest binding mismatch')
                else:
                    if set(body) != {'sequence', 'previous_sha256', 'entry'}:
                        raise JournalError('unknown or missing record fields')
                    if body['previous_sha256'] != previous_digest:
                        raise JournalError('journal hash chain mismatch')
                    entry = journal_from_json(_canonical(body['entry']))
                    _validate_entry(manifest, entries, completed, entry)
                    entries.append(entry)
                    if entry.state == 'COMPLETE':
                        completed.add(entry.action_id)
                previous_digest = digest
                sequence += 1
            except (ValueError, TypeError, UnicodeError, KeyError, RecursionError) as exc:
                raise ReconciliationRequired(
                    f'STOP: journal record {sequence} at byte {offset}: {exc}', entries) from exc
    if previous_digest is None:
        raise ReconciliationRequired('STOP: missing journal header')
    return tuple(entries)


def load_journal(path: Path, manifest: ExecutionManifest, *,
                 library_roots: tuple[Path, ...] = (),
                 forbidden_files: tuple[Path, ...] = ()) -> JournalSnapshot:
    """Inspect sealed history, never repair it or grant resumption authority."""
    path = Path(path)
    _header(manifest)
    parent_fd = _open_parent(path, manifest, library_roots, forbidden_files)
    fd = None
    primary = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent_fd)
        _check_file(fd, parent_fd, path.name)
        _lock(fd, False)
        _check_parent(path, parent_fd)
        before = os.fstat(fd)
        entries = _read_history(fd, manifest)
        after = _check_file(fd, parent_fd, path.name)
        _check_parent(path, parent_fd)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ReconciliationRequired('STOP: journal changed during inspection', entries)
        # Even a whole frame cannot prove the interrupted writer received fsync
        # acknowledgement, nor that its physical assertions still hold now.
        # A header-only prefix can also result from losing every event. The
        # loader therefore grants no resumption authority even for zero entries.
        return JournalSnapshot(manifest.manifest_id, entries, True)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _close_fds((fd, parent_fd), primary)


class DurableJournal:
    """One new journal and one live writer session; no existing-history resume API."""

    def __init__(self):
        raise TypeError('use DurableJournal.create for exclusive creation')

    @classmethod
    def create(cls, path: Path, manifest: ExecutionManifest, *,
               library_roots: tuple[Path, ...] = (),
               forbidden_files: tuple[Path, ...] = ()) -> DurableJournal:
        path = Path(path)
        raw, digest = _frame(_header(manifest))
        parent_fd = _open_parent(path, manifest, library_roots, forbidden_files)
        fd = None
        try:
            fd = os.open(path.name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
                | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
            _lock(fd, True)
            _check_file(fd, parent_fd, path.name)
            _write_all(fd, raw)
            os.fsync(fd)
            os.fsync(parent_fd)
            _check_parent(path, parent_fd)
            current = _check_file(fd, parent_fd, path.name)
            if current.st_size != len(raw):
                raise ReconciliationRequired('STOP: journal changed during creation')
            writer = object.__new__(cls)
            writer._fd = fd
            writer._parent_fd = parent_fd
            writer._path = path
            writer._manifest = manifest
            writer._entries: list[JournalEntry] = []
            writer._completed: set[str] = set()
            writer._digest = digest
            writer._stat = (current.st_size, current.st_mtime_ns)
            writer._pid = os.getpid()
            writer._mutex = threading.Lock()
            writer._poisoned = False
            writer._closed = False
            return writer
        except BaseException as exc:
            # Keep every created byte for diagnosis; no unlink/truncate/repair.
            _close_fds((fd, parent_fd), exc)
            raise

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        return tuple(self._entries)

    def inspect_live(self, manifest: ExecutionManifest) -> tuple[JournalEntry, ...]:
        """Read/check this live writer's exact binding and acknowledged history.

        This neither opens an existing writer nor grants restart authority.
        Bytes beyond acknowledged entries (even valid frames) require STOP.
        """
        if os.getpid() != self._pid:
            raise JournalError('writer inherited by another process')
        with self._mutex:
            if self._closed:
                raise JournalError('writer closed')
            if self._poisoned:
                raise ReconciliationRequired('STOP: interrupted writer requires reconciliation', self._entries)
            if _header(manifest) != _header(self._manifest):
                raise JournalError('live writer exact manifest binding mismatch')
            try:
                _check_parent(self._path, self._parent_fd)
                before = _check_file(self._fd, self._parent_fd, self._path.name)
                entries = _read_history(self._fd, manifest)
                after = _check_file(self._fd, self._parent_fd, self._path.name)
                _check_parent(self._path, self._parent_fd)
                if (entries != tuple(self._entries)
                        or (before.st_size, before.st_mtime_ns) != self._stat
                        or (after.st_size, after.st_mtime_ns) != self._stat):
                    raise ReconciliationRequired('STOP: live history differs from durable acknowledgements', entries)
                return entries
            except BaseException:
                self._poisoned = True
                raise

    def append(self, entry: JournalEntry) -> None:
        if os.getpid() != self._pid:
            raise JournalError('writer inherited by another process')
        with self._mutex:
            if self._poisoned:
                raise ReconciliationRequired('STOP: interrupted writer requires reconciliation', self._entries)
            if self._closed or os.getpid() != self._pid:
                raise JournalError('writer closed or inherited by another process')
            _validate_entry(self._manifest, self._entries, self._completed, entry)
            raw, digest = _frame({'sequence': len(self._entries) + 1,
                'previous_sha256': self._digest, 'entry': _strict_json_load(journal_to_json(entry))})
            try:
                _check_parent(self._path, self._parent_fd)
                current = _check_file(self._fd, self._parent_fd, self._path.name)
                if (current.st_size, current.st_mtime_ns) != self._stat:
                    raise ReconciliationRequired('STOP: journal changed outside writer', self._entries)
                _write_all(self._fd, raw)
                os.fsync(self._fd)
                _check_parent(self._path, self._parent_fd)
                current = _check_file(self._fd, self._parent_fd, self._path.name)
                if current.st_size != self._stat[0] + len(raw):
                    raise ReconciliationRequired('STOP: journal changed during append', self._entries)
            except BaseException:
                self._poisoned = True
                raise
            # Never acknowledge a frame until its write and durability barrier succeed.
            self._entries.append(entry)
            if entry.state == 'COMPLETE':
                self._completed.add(entry.action_id)
            self._digest = digest
            self._stat = (current.st_size, current.st_mtime_ns)

    def close(self) -> None:
        if os.getpid() != self._pid:
            # Close only this child's descriptor copies, without touching a
            # mutex possibly inherited locked from another parent thread.
            if not self._closed:
                self._closed = True
                _close_fds((self._fd, self._parent_fd))
            return
        with self._mutex:
            if not self._closed:
                self._closed = True
                _close_fds((self._fd, self._parent_fd))

    def __enter__(self) -> DurableJournal:
        if self._closed or os.getpid() != self._pid:
            raise JournalError('writer closed or inherited by another process')
        return self

    def __exit__(self, _type, primary, _traceback) -> None:
        try:
            self.close()
        except BaseException as exc:
            if primary is None:
                raise
            if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not isinstance(primary, (KeyboardInterrupt, SystemExit)):
                raise exc from primary
            notes = tuple(getattr(exc, '__notes__', ()))
            primary.add_note(f'journal STOP: cleanup failed: {type(exc).__name__}: {exc}')
            for note in notes:
                primary.add_note(note)
