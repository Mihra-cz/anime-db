"""E2A scratch-only single-action rename, with no executor or journal writes.

There is intentionally no API to open an existing execution/library root.
Caller controls scratch contents exclusively. No production mount is authorized.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import asdict, dataclass
import errno
from functools import lru_cache
import os
from pathlib import Path
import stat
import sys
import threading
import uuid

from .target_execution_manifest import (
    ExecutionManifest, ManifestAction, manifest_from_json, manifest_to_json,
    _canonical, _decode_type, _strict_json_load,
)
from .target_planner import relative_locator_safe
from .target_execution_mounts import _check_mounts, _mount_id, _read_mounts

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_RENAME_NOREPLACE = 1
_KNOWN_REJECTIONS = frozenset({errno.EEXIST, errno.EXDEV, errno.ENOSYS, errno.EOPNOTSUPP,
    errno.EINVAL, errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT, errno.ENOTDIR,
    errno.EISDIR, errno.ELOOP, errno.ENAMETOOLONG, errno.ENOTEMPTY})


def _close_evidence_fd(fd: int, primary: BaseException | None = None) -> None:
    """Preserve an active failure/signal when discarding read-only evidence.

    Never retry close: the descriptor may already have been released.
    """
    try:
        os.close(fd)
    except BaseException as exc:
        if primary is None:
            raise
        primary.add_note(f'E2A STOP: descriptor cleanup failed: {type(exc).__name__}: {exc}')


@contextmanager
def _held_evidence_fd(fd: int):
    try:
        yield fd
    except BaseException as exc:
        _close_evidence_fd(fd, exc)
        raise
    else:
        _close_evidence_fd(fd)


class RenameRejected(ValueError):
    """STOP. No continuation authority or journal failure evidence is granted."""

    def __init__(self, message: str, error_number: int | None = None):
        super().__init__(message)
        self.errno = error_number


class RenameReconciliationRequired(RenameRejected):
    """STOP: a syscall was attempted; no automatic retry is safe."""

    def __init__(self, message: str, *, syscall_succeeded: bool | None):
        super().__init__(message)
        self.syscall_succeeded = syscall_succeeded
        self.durability_confirmed = False


@dataclass(frozen=True)
class RenameResult:
    manifest_id: str
    action_id: str
    outcome: str
    syscall_succeeded: bool
    durability_confirmed: bool


@dataclass(frozen=True)
class ScratchVerification:
    manifest_id: str
    action_id: str
    device: int
    inode: int
    size: int
    mtime_ns: int


class ScratchObservation:
    """Session-minted read-only evidence; held inode prevents inode reuse.

    Valid only inside ScratchRenameSession.observe's context. No root adoption,
    journal assertions, rename, or recovery capability is exposed here.
    """

    def __init__(self):
        raise TypeError('use live ScratchRenameSession.observe')

    def _check(self, session, manifest, action) -> None:
        if (self._closed or os.getpid() != self._pid or session is not self._session
                or session._closed or session._poisoned
                or manifest != self._manifest or action != self._action):
            raise RenameRejected('invalid or expired scratch observation binding')
        session._check_root()
        session._parent_binding(_locator(action.source), self._source_parent)
        session._parent_binding(_locator(action.target), self._target_parent)
        session._check_fd(self._source_fd)
        held = os.fstat(self._source_fd)
        if (_identity(held) != _identity(self._before) or held.st_nlink != 1
                or held.st_size != self._before.st_size or held.st_mtime_ns != self._before.st_mtime_ns):
            raise RenameRejected('observed source inode or metadata changed')

    def check_before(self, session, manifest, action, source_fd, source_parent, target_parent) -> None:
        """Constrain the actual syscall's descriptors to original observed evidence."""
        self._check(session, manifest, action)
        if (_identity(os.fstat(source_fd)) != _identity(self._before)
                or _identity(os.fstat(source_parent)) != _identity(os.fstat(self._source_parent))
                or _identity(os.fstat(target_parent)) != _identity(os.fstat(self._target_parent))):
            raise RenameRejected('pre-rename observation identity mismatch')

    def verify(self) -> ScratchVerification:
        """Independently reopen paths and target after rename; perform no writes."""
        session, action = self._session, self._action
        if os.getpid() != self._pid:
            raise RenameRejected('scratch observation inherited by another process')
        with session._mutex:
            self._check(session, self._manifest, action)
            with ExitStack() as stack:
                source_parts, target_parts = _locator(action.source), _locator(action.target)
                source_parent = session._parent(source_parts, stack)
                target_parent = session._parent(target_parts, stack)
                _absent(source_parent, source_parts[-1])
                target_fd = os.open(target_parts[-1], _FILE, dir_fd=target_parent)
                stack.enter_context(_held_evidence_fd(target_fd))
                session._check_fd(target_fd)
                current = _check_source(target_fd, target_parent, target_parts[-1], action, self._before)
                self._check(session, self._manifest, action)
                _absent(source_parent, source_parts[-1])
                _check_source(target_fd, target_parent, target_parts[-1], action, self._before)
                return ScratchVerification(self._manifest.manifest_id, action.action_id,
                    current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)


@lru_cache(maxsize=1)
def _load_renameat2():
    if sys.platform != 'linux':
        raise OSError(errno.ENOSYS, 'Linux renameat2 required')
    try:
        # libc selects the architecture ABI (including ARM64); no syscall numbers.
        call = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise OSError(errno.ENOSYS, 'libc renameat2 unavailable') from exc
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    call.restype = ctypes.c_int
    return call


def _rename_noreplace(source_fd: int, source: str, target_fd: int, target: str) -> None:
    call = _load_renameat2()
    if call(source_fd, os.fsencode(source), target_fd, os.fsencode(target), _RENAME_NOREPLACE) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _secure_directory(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise RenameRejected('directory must be owned by caller and not group/world writable')


def _absolute(path: Path) -> Path:
    raw = str(path)
    if (not path.is_absolute() or raw != path.as_posix() or '\x00' in raw
            or '\\' in raw or '..' in path.parts or raw.startswith('//')):
        raise RenameRejected('absolute canonical scratch path required')
    return path


def _open_absolute(path: Path) -> int:
    path = _absolute(path)
    fd = os.open('/', _DIRECTORY)
    try:
        for i, part in enumerate(path.parts[1:], 1):
            child = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            # /tmp is sticky shared storage; every directory below it is private
            # to the caller's tree (other same-UID writers remain out of scope).
            if path.parts[1] == 'tmp' and i > 1:
                _secure_directory(fd)
        return fd
    except BaseException as exc:
        _close_evidence_fd(fd, exc)
        raise


def _locator(raw: str) -> tuple[str, ...]:
    if (type(raw) is not str or not relative_locator_safe(raw) or '\x00' in raw
            or any(p.casefold() == '#recycle' for p in raw.split('/'))):
        raise RenameRejected('unsafe action relative path')
    return tuple(raw.split('/'))


def _check_source(fd: int, parent: int, name: str, action: ManifestAction,
                  expected: os.stat_result | None = None) -> os.stat_result:
    info = os.fstat(fd)
    named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(named.st_mode)
            or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o022
            or _identity(info) != _identity(named)
            or (info.st_size, info.st_mtime_ns) != (action.expected_size, action.expected_mtime_ns)
            or (named.st_size, named.st_mtime_ns) != (action.expected_size, action.expected_mtime_ns)
            or (expected is not None and _identity(info) != _identity(expected))):
        raise RenameRejected('source identity, regular-file or size/mtime precondition failed')
    return info


def _absent(parent: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise RenameRejected('target already exists; STOP', errno.EEXIST)


class ScratchRenameSession:
    """An unreopenable fresh scratch root. Each syscall action is attempted once.

    The mutex serializes local use, not global execution. Same-UID/privileged
    mutations are outside the exclusive scratch ownership precondition.
    """

    def __init__(self, *_args, **_kwargs):
        raise TypeError('use ScratchRenameSession.create; existing roots cannot be opened')

    @classmethod
    def create(cls, parent: Path) -> ScratchRenameSession:
        """Explicitly create a new empty 0700 root inside a caller's /tmp tree.

        This is scratch allocation, not implicit creation of target parents.
        No production mode, production capability token, or reopen API exists.
        """
        parent = _absolute(Path(parent))
        if not parent.is_relative_to(Path('/tmp')) or parent == Path('/tmp'):
            raise RenameRejected('scratch parent must be an existing private directory under /tmp')
        parent_fd = root_fd = None
        try:
            _load_renameat2()
            parent_fd = _open_absolute(parent)
            _secure_directory(parent_fd)
            mounts = _read_mounts()
            root = parent / ('v6-e2a-' + uuid.uuid4().hex)
            _check_mounts(root, parent_fd, mounts)
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
            root_fd = os.open(root.name, _DIRECTORY, dir_fd=parent_fd)
            _secure_directory(root_fd)
            current = _check_mounts(root, root_fd, mounts)
            if _read_mounts() != mounts:
                raise RenameRejected('mount table changed during scratch allocation')
            session = object.__new__(cls)
            session._root = root
            session._root_fd = root_fd
            session._root_identity = _identity(os.fstat(root_fd))
            session._mounts = mounts
            session._mount_id = current.mount_id
            info = os.fstat(root_fd)
            session._mount_identity = f'{current.fstype}:{current.source}:{info.st_dev}:{info.st_ino}'
            session._pid = os.getpid()
            session._mutex = threading.Lock()
            session._attempted: set[tuple[str, str]] = set()
            session._poisoned: bool = False
            session._closed = False
            root_fd = None  # Ownership transferred; allocation never removes artifacts.
            return session
        except (OSError, ValueError) as exc:
            if isinstance(exc, RenameRejected):
                raise
            raise RenameRejected(str(exc), getattr(exc, 'errno', None)) from exc
        finally:
            if root_fd is not None:
                os.close(root_fd)
            if parent_fd is not None:
                os.close(parent_fd)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def mount_identity(self) -> str:
        """Observed scratch binding only; never a production capability assertion."""
        return self._mount_identity

    def _check_root(self) -> None:
        if _read_mounts() != self._mounts:
            raise RenameRejected('mount table changed; capability must not be reused')
        # Every action path is resolved from this held descriptor, not the root path.
        if (_identity(os.fstat(self._root_fd)) != self._root_identity
                or _mount_id(self._root_fd) != self._mount_id):
            raise RenameRejected('held execution root descriptor changed')
        _check_mounts(self.root, self._root_fd, self._mounts)
        fd = _open_absolute(self.root)
        try:
            if (_identity(os.fstat(fd)) != self._root_identity
                    or _mount_id(fd) != self._mount_id):
                raise RenameRejected('execution root binding changed')
        except BaseException as exc:
            _close_evidence_fd(fd, exc)
            raise
        else:
            _close_evidence_fd(fd)
        _secure_directory(self._root_fd)

    def _check_fd(self, fd: int) -> None:
        if os.fstat(fd).st_dev != self._root_identity[0]:
            raise RenameRejected('cross-filesystem operation forbidden', errno.EXDEV)
        if _mount_id(fd) != self._mount_id:
            raise RenameRejected('mount crossing forbidden')

    def _parent(self, parts: tuple[str, ...], stack: ExitStack) -> int:
        fd = self._root_fd
        for part in parts[:-1]:
            fd = os.open(part, _DIRECTORY, dir_fd=fd)
            stack.enter_context(_held_evidence_fd(fd))
            _secure_directory(fd)
            self._check_fd(fd)
        return fd

    def _parent_binding(self, parts: tuple[str, ...], held: int) -> None:
        with ExitStack() as stack:
            current = self._parent(parts, stack)
            if _identity(os.fstat(current)) != _identity(os.fstat(held)):
                raise RenameRejected('action parent binding changed')
            _secure_directory(held)
            self._check_fd(held)

    def _validate(self, manifest: ExecutionManifest, action: ManifestAction) -> None:
        if type(manifest) is not ExecutionManifest or type(action) is not ManifestAction:
            raise RenameRejected('typed immutable manifest and action required')
        # Reject a foreign root before the existing validator's read-only lstat
        # checks: even an invalid production proposal must not observe the NAS.
        if (manifest.payload.library_root != str(self.root)
                or manifest.payload.mount_identity != self.mount_identity):
            raise RenameRejected('manifest execution root/mount binding mismatch')
        # The existing strict decoder additionally rejects permissive dataclass
        # values such as bool instead of int, without changing the E1 contracts.
        try:
            manifest_from_json(manifest_to_json(manifest))
            _decode_type(ManifestAction, _strict_json_load(_canonical(asdict(action))))
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            raise RenameRejected('invalid manifest: ' + str(exc)) from exc
        if action not in manifest.payload.actions:
            raise RenameRejected('action does not exactly match sealed manifest')
        if action.action not in {'KEEP', 'MOVE', 'QUARANTINE'} or action.criticality == 'REVIEW':
            raise RenameRejected('action requires review or is unsupported')
        if manifest.payload.generation_diagnostics or action.depends_on:
            raise RenameRejected('primitive cannot authorize unresolved plan or dependencies')
        _locator(action.source)
        _locator(action.target)

    @contextmanager
    def observe(self, manifest: ExecutionManifest, action: ManifestAction):
        """Capture read-only evidence for this live scratch session only."""
        if os.getpid() != self._pid:
            raise RenameRejected('scratch session inherited by another process')
        with ExitStack() as stack:
            with self._mutex:
                if self._closed or self._poisoned:
                    raise RenameRejected('scratch session unavailable for observation')
                self._validate(manifest, action)
                if action.action not in {'MOVE', 'QUARANTINE'}:
                    raise RenameRejected('observation requires MOVE or QUARANTINE')
                self._check_root()
                source_parts, target_parts = _locator(action.source), _locator(action.target)
                source_parent = self._parent(source_parts, stack)
                target_parent = self._parent(target_parts, stack)
                source_fd = os.open(source_parts[-1], _FILE, dir_fd=source_parent)
                stack.enter_context(_held_evidence_fd(source_fd))
                self._check_fd(source_fd)
                before = _check_source(source_fd, source_parent, source_parts[-1], action)
                _absent(target_parent, target_parts[-1])
                self._check_root()
                self._parent_binding(source_parts, source_parent)
                self._parent_binding(target_parts, target_parent)
                evidence = object.__new__(ScratchObservation)
                evidence._session, evidence._manifest, evidence._action = self, manifest, action
                evidence._source_parent, evidence._target_parent = source_parent, target_parent
                evidence._source_fd, evidence._before = source_fd, before
                evidence._pid, evidence._closed = os.getpid(), False
            try:
                yield evidence
            finally:
                evidence._closed = True

    def rename(self, manifest: ExecutionManifest, action: ManifestAction, *,
               observation: ScratchObservation | None = None) -> RenameResult:
        if os.getpid() != self._pid:
            raise RenameRejected('scratch session inherited by another process')
        with self._mutex:
            if self._poisoned:
                raise RenameReconciliationRequired('session requires reconciliation; no retry', syscall_succeeded=None)
            if self._closed:
                raise RenameRejected('scratch session closed')
            self._validate(manifest, action)
            key = (manifest.manifest_id, action.action_id)
            if key in self._attempted:
                raise RenameRejected('action already attempted; no retry')
            attempted = succeeded = False
            try:
                with ExitStack() as stack:
                    self._check_root()
                    source_parts, target_parts = _locator(action.source), _locator(action.target)
                    source_parent = self._parent(source_parts, stack)
                    target_parent = self._parent(target_parts, stack)
                    source_fd = os.open(source_parts[-1], _FILE, dir_fd=source_parent)
                    stack.enter_context(_held_evidence_fd(source_fd))
                    self._check_fd(source_fd)
                    before = _check_source(source_fd, source_parent, source_parts[-1], action)
                    if action.action != 'KEEP':
                        _absent(target_parent, target_parts[-1])
                    self._check_root()
                    self._parent_binding(source_parts, source_parent)
                    self._parent_binding(target_parts, target_parent)
                    _check_source(source_fd, source_parent, source_parts[-1], action, before)
                    if observation is not None:
                        if type(observation) is not ScratchObservation:
                            raise RenameRejected('session-minted scratch observation required')
                        observation.check_before(self, manifest, action, source_fd, source_parent, target_parent)
                    if action.action == 'KEEP':
                        return RenameResult(manifest.manifest_id, action.action_id, 'KEEP', False, False)
                    self._attempted.add(key)
                    attempted = True
                    try:
                        _rename_noreplace(source_parent, source_parts[-1], target_parent, target_parts[-1])
                    except OSError as exc:
                        if exc.errno not in _KNOWN_REJECTIONS:
                            raise
                        # A known errno is insufficient if current source binding
                        # changed or the target is the original source inode.
                        self._check_root()
                        self._parent_binding(source_parts, source_parent)
                        self._parent_binding(target_parts, target_parent)
                        _check_source(source_fd, source_parent, source_parts[-1], action, before)
                        try:
                            named = os.stat(target_parts[-1], dir_fd=target_parent, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            if _identity(named) == _identity(before):
                                raise RenameReconciliationRequired('target aliases source after syscall error',
                                    syscall_succeeded=None) from exc
                        # A validated syscall rejection is still STOP, not LOW
                        # continuation authority; E1 integration is a later phase.
                        attempted = False
                        raise RenameRejected('rename syscall rejected; STOP', exc.errno) from exc
                    succeeded = True
                    self._verify_applied(action, source_fd, source_parent, target_parent, before)
                    # File first, then destination name, then removal of old name.
                    # Both directories need barriers for a cross-directory rename.
                    os.fsync(source_fd)
                    os.fsync(target_parent)
                    if _identity(os.fstat(source_parent)) != _identity(os.fstat(target_parent)):
                        os.fsync(source_parent)
                    self._verify_applied(action, source_fd, source_parent, target_parent, before)
                return RenameResult(manifest.manifest_id, action.action_id, 'DURABLE', True, True)
            except BaseException as exc:
                if attempted:
                    self._poisoned = True
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        exc.add_note('E2A STOP: syscall attempted; session blocked; reconciliation required')
                        raise
                    raise RenameReconciliationRequired('filesystem outcome requires reconciliation: ' + str(exc),
                        syscall_succeeded=True if succeeded else None) from exc
                if isinstance(exc, RenameRejected):
                    raise
                if isinstance(exc, (OSError, ValueError)):
                    raise RenameRejected(str(exc), getattr(exc, 'errno', None)) from exc
                raise

    def _verify_applied(self, action: ManifestAction, source_fd: int, source_parent: int,
                        target_parent: int, before: os.stat_result) -> None:
        self._check_root()
        self._parent_binding(_locator(action.source), source_parent)
        self._parent_binding(_locator(action.target), target_parent)
        _absent(source_parent, _locator(action.source)[-1])
        _check_source(source_fd, target_parent, _locator(action.target)[-1], action, before)

    def close(self) -> None:
        if os.getpid() != self._pid:
            if not self._closed:
                self._closed = True
                os.close(self._root_fd)
            return
        with self._mutex:
            if not self._closed:
                self._closed = True
                os.close(self._root_fd)

    def __enter__(self) -> ScratchRenameSession:
        if self._closed or os.getpid() != self._pid:
            raise RenameRejected('scratch session closed or inherited')
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
