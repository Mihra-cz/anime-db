"""E6B: opt-in, same-host cooperative exclusion for explicit local scratch data.

A free advisory lock is never evidence of a safe FS/DB state or resume authority.
The fixed namespace is not removed on release. Noncooperating writers are outside.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import fcntl
from functools import wraps
from inspect import signature
import json
import os
from pathlib import Path
import stat
import threading

from sqlalchemy.engine import make_url

from .target_execution_filesystem import _open_absolute, _held_evidence_fd
from .target_execution_mounts import _check_mounts, _read_mounts


class MaintenanceStopped(RuntimeError):
    """STOP: admission/ownership is unconfirmed; never retry this operation."""


class MaintenanceBusy(MaintenanceStopped):
    """A cooperating writer or exclusive maintenance owner prevents admission."""


_registry_lock = threading.RLock()
_descriptors = set()
_owner_scope = ContextVar('animedb_maintenance_owner', default=None)
_readonly_scope = ContextVar('animedb_readonly_request', default=False)


def _before_fork():
    _registry_lock.acquire()


def _after_parent():
    _registry_lock.release()


def _after_fork():
    global _registry_lock
    # Closing the child's copy must not issue LOCK_UN on the parent's OFD.
    for lease in tuple(_descriptors):
        try:
            os.close(lease._fd)
        except OSError:
            pass
        lease._closed = True
    _descriptors.clear()
    _registry_lock = threading.RLock()
    _owner_scope.set(None)


os.register_at_fork(before=_before_fork,
    after_in_parent=_after_parent, after_in_child=_after_fork)


class _Lease:
    def __init__(self, boundary, flags, exclusive=False):
        self.boundary, self.exclusive = boundary, exclusive
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._closed = self._closing = False
        self._borrowers = 0
        with _registry_lock:
            self._fd = os.open(boundary.lock_path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
            _descriptors.add(self)

    def check(self):
        if self._pid != os.getpid() or self._closed or self._closing:
            raise MaintenanceStopped('STOP: inherited, closed or ending maintenance lease')
        info, named = os.fstat(self._fd), self.boundary.lock_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
                or not stat.S_ISREG(named.st_mode)):
            raise MaintenanceStopped('STOP: maintenance namespace identity/permissions changed')
        return info.st_dev, info.st_ino

    def _close_fd(self):
        with _registry_lock:
            self._closed = True
            _descriptors.discard(self)
            os.close(self._fd)  # Never retry a close with uncertain acknowledgement.

    def close(self):
        if self._pid != os.getpid():
            raise MaintenanceStopped('STOP: inherited lease has no close authority')
        with _registry_lock:
            if self._closed:
                return
            if self._borrowers:
                self._closing = True
                raise MaintenanceStopped('STOP: maintenance still owns an active DB transaction')
            self._close_fd()

    @contextmanager
    def write_scope(self):
        self.check()
        if not self.exclusive or self._thread != threading.get_ident():
            raise MaintenanceStopped('STOP: exclusive owner thread required')
        token = _owner_scope.set(self)
        try:
            yield
        finally:
            _owner_scope.reset(token)


class _BorrowedLease:
    def __init__(self, owner):
        self.owner, self._closed, self._pid = owner, False, os.getpid()
        with _registry_lock:
            owner.check()
            owner._borrowers += 1

    def close(self):
        if self._pid != os.getpid():
            raise MaintenanceStopped('STOP: inherited DB admission has no close authority')
        with _registry_lock:
            if not self._closed:
                self._closed = True
                self.owner._borrowers -= 1
                if self.owner._closing and not self.owner._borrowers:
                    self.owner._close_fd()


class ScratchMaintenance:
    """One immutable SQLite/root binding; no environment or production activation."""

    def __init__(self, database_path: Path, library_root: Path):
        self.database_path, self.library_root = Path(database_path), Path(library_root)
        self._pid = os.getpid()
        for path in (self.database_path, self.library_root):
            if (not path.is_absolute() or not path.is_relative_to('/tmp')
                    or '..' in path.parts or path == Path('/tmp')):
                raise MaintenanceStopped('STOP: explicit absolute scratch paths under /tmp required')
        if not self.library_root.is_relative_to(self.database_path.parent):
            raise MaintenanceStopped('STOP: scratch library must be in the private database directory')
        self.lock_path = self.database_path.with_name(self.database_path.name + '.maintenance.lock')
        self._binding = (json.dumps({'version': 1, 'database': str(self.database_path),
            'library': str(self.library_root)}, sort_keys=True) + '\n').encode()
        self._check_paths()
        lease = None
        try:
            try:
                lease = _Lease(self, os.O_RDWR | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                lease = _Lease(self, os.O_RDWR)
            else:
                fcntl.flock(lease._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if os.write(lease._fd, self._binding) != len(self._binding):
                    raise MaintenanceStopped('STOP: incomplete maintenance namespace binding')
                os.fsync(lease._fd)
                with _held_evidence_fd(_open_absolute(self.lock_path.parent)) as parent:
                    os.fsync(parent)
            self._identity = lease.check()
            self._check_binding(lease)
        except Exception as exc:
            if isinstance(exc, MaintenanceStopped):
                raise
            raise MaintenanceStopped('STOP: maintenance namespace unavailable') from exc
        finally:
            if lease is not None:
                lease.close()

    def _check_paths(self):
        self.check_process()
        mounts = _read_mounts()
        for path in (self.database_path.parent, self.library_root):
            with _held_evidence_fd(_open_absolute(path)) as fd:
                _check_mounts(path, fd, mounts)
        try:
            info = self.database_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_mode & 0o022):
                raise MaintenanceStopped('STOP: private regular scratch SQLite required')

    def check_process(self):
        if self._pid != os.getpid():
            raise MaintenanceStopped('STOP: reconstruct the scratch binding after fork')

    def _check_binding(self, lease):
        if (lease.check() != self._identity
                or os.pread(lease._fd, len(self._binding) + 1, 0) != self._binding):
            raise MaintenanceStopped('STOP: conflicting or replaced maintenance namespace')

    def validate_engine_url(self, database_url):
        url = make_url(database_url)
        if url.drivername != 'sqlite' or url.database != str(self.database_path) or url.query:
            raise MaintenanceStopped('STOP: engine does not match explicit scratch SQLite')
        self._check_paths()

    def validate_root(self, root):
        if Path(root) != self.library_root:
            raise MaintenanceStopped('STOP: scanner/root binding differs from maintenance namespace')

    def validate_artwork_root(self, root):
        path = Path(root)
        if (not path.is_absolute() or '..' in path.parts
                or not path.is_relative_to(self.database_path.parent)):
            raise MaintenanceStopped('STOP: artwork must remain inside the private scratch tree')
        self._check_paths()
        # Existing ancestors must be private directories, never symlink aliases.
        # mkdir may create the remaining suffix only after writer admission.
        while True:
            try:
                path.lstat()
            except FileNotFoundError:
                path = path.parent
                continue
            try:
                with _held_evidence_fd(_open_absolute(path)) as fd:
                    _check_mounts(path, fd, _read_mounts())
            except Exception as exc:
                raise MaintenanceStopped('STOP: artwork directory binding is unsafe') from exc
            break

    def _acquire(self, exclusive=False):
        self._check_paths()
        owner = _owner_scope.get()
        if not exclusive and owner is not None:
            owner.check()
            if (owner.boundary._binding != self._binding
                    or owner._thread != threading.get_ident()):
                raise MaintenanceStopped('STOP: foreign exclusive owner DB scope')
            return _BorrowedLease(owner)
        lease = None
        try:
            lease = _Lease(self, os.O_RDWR, exclusive)
            self._check_binding(lease)
            try:
                fcntl.flock(lease._fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MaintenanceBusy('STOP: cooperating writer/maintenance is active') from exc
            self._check_binding(lease)
            return lease
        except BaseException as exc:
            if lease is not None:
                try:
                    lease.close()
                except BaseException as cleanup:
                    exc.add_note(f'E6B STOP: acquisition cleanup failed: {cleanup}')
            if isinstance(exc, Exception) and not isinstance(exc, MaintenanceStopped):
                raise MaintenanceStopped('STOP: writer/maintenance admission unconfirmed') from exc
            raise

    @contextmanager
    def _held(self, exclusive):
        lease = self._acquire(exclusive)
        primary = None
        try:
            yield lease
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                if isinstance(lease, _Lease):
                    self._check_binding(lease)
            except BaseException as exc:
                if primary is None:
                    primary = exc
                    raise
                primary.add_note(f'E6B STOP: namespace verification failed: {exc}')
            finally:
                try:
                    lease.close()
                except BaseException as exc:
                    if primary is None:
                        raise
                    primary.add_note(f'E6B STOP: lease close failed: {exc}')

    def writer(self):
        return self._held(False)

    def maintenance(self):
        return self._held(True)


@contextmanager
def readonly_request():
    token = _readonly_scope.set(True)
    try:
        yield
    finally:
        _readonly_scope.reset(token)


def maintenance_for(bind):
    # Session and Connection delegate to the explicitly configured engine only.
    engine = bind.get_bind() if hasattr(bind, 'get_bind') else bind
    engine = getattr(engine, 'engine', engine)
    return engine.get_execution_options().get('scratch_maintenance')


def writer_admission(bind):
    gate = maintenance_for(bind)
    return gate.writer() if gate is not None else nullcontext()


def protected_writer(function):
    parameters = signature(function)
    first_parameter = next(iter(parameters.parameters))

    @wraps(function)
    def guarded(*args, **kwargs):
        bound = parameters.bind(*args, **kwargs)
        with writer_admission(bound.arguments[first_parameter]):
            return function(*args, **kwargs)
    return guarded


class MaintenanceMiddleware:
    """Lease covers the whole mutating request, including sync handlers/cleanup."""

    def __init__(self, app, maintenance):
        self.app, self.maintenance = app, maintenance

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        if scope['method'] in {'GET', 'HEAD', 'OPTIONS'}:
            with readonly_request():
                return await self.app(scope, receive, send)
        admission = self.maintenance.writer()
        try:
            admission.__enter__()
        except MaintenanceStopped:
            from starlette.responses import JSONResponse
            return await JSONResponse({'detail': 'Maintenance: write admission refused.'}, status_code=503)(scope, receive, send)
        primary = None
        try:
            await self.app(scope, receive, send)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            admission.__exit__(type(primary) if primary else None,
                primary, primary.__traceback__ if primary else None)
