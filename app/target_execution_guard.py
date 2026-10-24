"""R3 persistent STOP latch for the original scratch E7 owner.

No completion/release/recovery API. Valid or damaged evidence denies ordinary
scanning; neither a free lock nor a loaded COMPLETE journal admits a scan.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit

from sqlalchemy.util import asbool

from . import target_execution_journal as journal
from .maintenance import MaintenanceStopped, ScratchMaintenance, maintenance_for
from .target_execution_manifest import manifest_to_json


class ExecutionGuardStopped(MaintenanceStopped):
    """Reorganization requires inspection before a write-capable scan/startup."""


def _stop(detail):
    return ExecutionGuardStopped(f'STOP: Reorganization requires review: {detail}')


def _guard_path(database):
    return database.with_name(database.name + '.execution-guard.json')


def _present(path):
    try:
        path.lstat()  # Broken symlinks and nonregular evidence also mean STOP.
    except FileNotFoundError:
        return False
    return True


def _library_path(root):
    # ANIME_PATH may be an unavailable CIFS mount. Never resolve/stat it here;
    # only the scanner's separate healthcheck process may establish access.
    root = Path(root)
    if ('..' in root.parts or '\x00' in str(root) or '\\' in str(root)
            or root.anchor == '//'):
        raise _stop('unsafe library path')
    return root.absolute()


def _scratch_library_root(database):
    if (database.is_relative_to('/tmp') and database.suffix == '.sqlite'
            and re.fullmatch(r'v6-e2a-[0-9a-f]{32}', database.stem)):
        return database.with_suffix('')
    return None


def _scratch_evidence_present(database):
    root = _scratch_library_root(database)
    if root is None:
        return False
    suffixes = ('.journal.jsonl', '.directories.jsonl', '.cleanup.jsonl', '.identity.json')
    return (any(_present(root.parent / (root.name + suffix)) for suffix in suffixes)
        or any(root.parent.glob(root.name + '.identity-*.json')))


def _require_no_evidence(database):
    # This fixed DB namespace is a denial latch, never permission based on JSON
    # contents. Keeping it after success avoids inventing restart authority.
    if _present(_guard_path(database)):
        raise _stop('persistent execution guard is present (possibly incomplete or damaged)')
    # Original scratch owners use these exact adjacent names. Missing R3 bytes
    # must not admit scans of a known E1/E7 operation, including older executions.
    if _scratch_evidence_present(database):
        raise _stop('execution evidence remains without its persistent guard')


class _ScratchExecutionGuard:
    """Pinned immutable marker; only the live exclusive owner may create it."""

    @classmethod
    def create(cls, database, root, manifest, maintenance_owner):
        from .target_execution_rollback import _owner
        _owner(maintenance_owner, root)
        gate = maintenance_owner.boundary
        if database != gate.database_path or manifest.payload.library_root != str(root):
            raise _stop('guard database/library/manifest binding differs')
        _require_no_evidence(database)
        path = _guard_path(database)
        parent = journal._open_parent(path, manifest, (), ())
        fd = None
        try:
            body = {
                'schema': 'animedb-v6-scratch-execution-guard', 'version': 1,
                'database': str(database), 'database_identity': list(journal._identity(database.stat())),
                'library': str(root), 'library_identity': list(journal._identity(root.stat())),
                'manifest_id': manifest.manifest_id,
                'manifest_sha256': hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest(),
                'initial_db_fingerprint': asdict(manifest.payload.db_fingerprint),
                'journal': str(root.parent / (root.name + '.journal.jsonl')),
            }
            raw, _ = journal._frame(body)
            fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL
                | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            journal._check_file(fd, parent, path.name)
            journal._write_all(fd, raw)
            os.fsync(fd)
            os.fsync(parent)
            guard = cls()
            guard.path, guard._fd, guard._parent, guard._raw = path, fd, parent, raw
            guard._database, guard._root, guard._body = database, root, body
            guard.check()
            gate._check_binding(maintenance_owner)
            return guard
        except BaseException as exc:
            # Preserve even partial/uncertain bytes. No retry and no unlink.
            journal._close_fds((fd, parent), exc)
            raise

    def check(self):
        journal._check_parent(self.path, self._parent)
        info = journal._check_file(self._fd, self._parent, self.path.name)
        if (info.st_size != len(self._raw)
                or os.pread(self._fd, len(self._raw) + 1, 0) != self._raw
                or list(journal._identity(self._database.stat())) != self._body['database_identity']
                or list(journal._identity(self._root.stat())) != self._body['library_identity']):
            raise _stop('live persistent execution guard changed')

    def close(self):
        journal._close_fds((self._fd, self._parent))


def _database_path(bind):
    engine = bind.get_bind() if hasattr(bind, 'get_bind') else bind
    engine = getattr(engine, 'engine', engine)
    url = engine.url
    name = url.database
    if url.get_backend_name() != 'sqlite' or not name or name == ':memory:':
        return None
    if asbool(url.query.get('uri', False)) and name.startswith('file:'):
        if url.query.get('mode') == 'memory' or name == 'file::memory:':
            return None
        uri = urlsplit(name)
        if uri.netloc not in {'', 'localhost'}:
            raise _stop('unknown SQLite URI authority')
        name = unquote(uri.path)
    # Reopening the same SQLite through relative/symlink/URI paths must still
    # find its fixed denial namespace. This does not open a DB connection.
    database = Path(name).resolve()
    if database.is_relative_to('/tmp'):
        try:
            info = database.stat()
        except FileNotFoundError:
            return database
        # Preserve the original same-directory hardlink guard without rejecting
        # ordinary hardlinks. Only an exact inode match AND existing scratch
        # evidence can relate an alias to the original local DB namespace.
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            for candidate in database.parent.glob('v6-e2a-*.sqlite'):
                if candidate == database or _scratch_library_root(candidate) is None:
                    continue
                current = candidate.lstat()
                if (stat.S_ISREG(current.st_mode)
                        and journal._identity(current) == journal._identity(info)
                        and (_present(_guard_path(candidate))
                            or _present(candidate.with_name(candidate.name + '.maintenance.lock'))
                            or _scratch_evidence_present(candidate))):
                    return candidate
    return database


def _existing_maintenance_root(database):
    # Discovery is local E6B only. A similarly named production sidecar does
    # not activate ScratchMaintenance's /tmp, mount, file-type or nlink policy.
    if not database.is_relative_to('/tmp'):
        return None
    lock = database.with_name(database.name + '.maintenance.lock')
    if not _present(lock):
        return None
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _stop('maintenance binding is not a regular file')
        value = journal._strict_json_load(os.pread(fd, 65536, 0).decode('utf-8'))
        if (type(value) is not dict or set(value) != {'version', 'database', 'library'}
                or type(value['version']) is not int or value['version'] != 1
                or value['database'] != str(database) or type(value['library']) is not str):
            raise _stop('conflicting maintenance binding')
        root = _library_path(value['library'])
        if not root.is_relative_to(database.parent) or not Path(value['library']).is_absolute():
            raise _stop('maintenance library escapes its scratch database directory')
        # This is a candidate binding only. ScratchMaintenance independently
        # verifies canonical bytes, ownership, nofollow paths, mounts and flock.
        return root
    finally:
        os.close(fd)


@contextmanager
def ordinary_scan_admission(bind, root):
    """Check before SQL/autoflush/probe; retain cooperating exclusion to commit.

    Discovery of an existing scratch boundary works in a new plain engine too.
    This does not activate a production executor or protect noncooperating SQL.
    """
    with ExitStack() as admissions:
        try:
            root = _library_path(root)
            database = _database_path(bind)
            if database is not None:
                _require_no_evidence(database)
            gate = maintenance_for(bind)
            if gate is None and database is not None:
                bound_root = _scratch_library_root(database)
                if bound_root is None:
                    bound_root = _existing_maintenance_root(database)
                if bound_root is not None:
                    if root != bound_root:
                        raise _stop('scanner root differs from the database execution binding')
                    gate = ScratchMaintenance(database, bound_root)
            if gate is not None:
                gate.validate_root(root)
                admissions.enter_context(gate.writer())
            if database is not None:
                _require_no_evidence(database)
        except MaintenanceStopped:
            raise
        except (OSError, ValueError) as exc:
            raise _stop('execution guard admission could not be verified') from exc
        yield
