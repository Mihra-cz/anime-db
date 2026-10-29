"""R3 persistent STOP latch for the original scratch E7 owner.

The STOP record is retained. An explicit live-verified completion may append
normal scanning authority; a free lock or loaded COMPLETE journal alone cannot.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit

from sqlalchemy.util import asbool

from . import target_execution_journal as journal
from .maintenance import MaintenanceStopped, ScratchMaintenance, maintenance_for
from .target_execution_manifest import manifest_to_json, manifest_from_json, JournalEntry


class ExecutionGuardStopped(MaintenanceStopped):
    """Reorganization requires inspection before a write-capable scan/startup."""


@dataclass(frozen=True)
class NormalOperation:
    """Verified terminal history; current identities/locators now belong to the DB."""

    manifest_id: str


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
    # Keep the original STOP namespace. Only its explicit live-verified terminal
    # transition may supply the manifest for ordinary scanner routing.
    if _present(_guard_path(database)):
        with _guard_state(database) as (state, *_):
            return state
    # Original scratch owners use these exact adjacent names. Missing R3 bytes
    # must not admit scans of a known E1/E7 operation, including older executions.
    if _scratch_evidence_present(database):
        raise _stop('execution evidence remains without its persistent guard')


class _ScratchExecutionGuard:
    """Pinned STOP prefix; only its live exclusive owner may append a transition."""

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
            guard._identity = journal._identity(os.fstat(fd))
            guard._transition_attempted = False
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

    def enable_scanning(self, owner):
        """Explicit transition, only while original COMPLETE evidence is live.

        Append to the existing guard, retaining the original STOP bytes. A torn
        append remains STOP; no deletion, journal replay or recovery is involved.
        """
        from .scanner.execution_completion import _check_live_completion
        if owner._persistent_guard is not self:
            raise _stop('original execution guard required')
        _check_live_completion(owner)
        self.check()
        targets = []
        for action in owner._manifest.payload.actions:
            info = (owner.root / action.target).lstat()
            targets.append([action.target, *journal._identity(info)])
        body = {'state': 'SCANNING', 'manifest': manifest_to_json(owner._manifest),
            'targets': targets}
        raw = (journal._canonical({**body, 'sha256': journal._digest(body)}) + '\n').encode()
        if len(self._raw) + len(raw) > _MAX_GUARD_BYTES:
            raise _stop('completed guard exceeds supported size')
        _check_live_completion(owner)
        self._transition_attempted = True
        os.lseek(self._fd, 0, os.SEEK_END)
        journal._write_all(self._fd, raw)
        os.fsync(self._fd)
        os.fsync(self._parent)
        self._raw += raw
        self.check()

    def stop_scanning(self, primary):
        """Retain STOP on any reported execution/finalization failure.

        Resources may already be closed. Reopen only this original guard under
        the caller's still-held exclusive maintenance lease; retain all bytes.
        """
        if not self._transition_attempted:
            return
        _revoke_transition(self.path, self._identity, primary)


def _revoke_transition(path, identity, primary):
    from .target_execution_filesystem import _open_absolute
    parent = fd = None
    try:
        parent = _open_absolute(path.parent)
        fd = os.open(path.name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
            | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        info = journal._check_file(fd, parent, path.name)
        if journal._identity(info) != identity:
            raise _stop('original guard replaced during failure handling')
        journal._write_all(fd, b'STOP: execution finalization failed\n')
        os.fsync(fd)
        os.fsync(parent)
    except Exception as exc:
        primary.add_note(f'Execution STOP: guard revocation unconfirmed: {exc}')
    finally:
        journal._close_fds((fd, parent), primary)


_MAX_GUARD_BYTES = 16 * 1024 * 1024


def _identity_history(manifest, parent):
    """Validate retained E7 identity records, without adopting execution authority."""
    from .target_execution_identity import _decode_frame, _MAX_RECORD_BYTES
    root = Path(manifest.payload.library_root)
    root_identity = journal._identity(root.stat())
    manifest_sha256 = hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest()
    result = []
    for index, action in enumerate(manifest.payload.actions):
        name = root.name + f'.identity-{index}.json'
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent)
        try:
            before = journal._check_file(fd, parent, name)
            raw = os.pread(fd, _MAX_RECORD_BYTES + 1, 0)
            evidence = _decode_frame(raw)
            if (evidence.manifest_id != manifest.manifest_id or evidence.action != action
                    or evidence.action_index != index
                    or evidence.manifest_sha256 != manifest_sha256
                    or evidence.binding.root != str(root)
                    or (evidence.binding.root_identity.st_dev, evidence.binding.root_identity.st_ino) != root_identity):
                raise _stop('retained identity evidence binding differs')
            after = journal._check_file(fd, parent, name)
            if (before.st_size != len(raw)
                    or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                raise _stop('identity history changed during admission')
            result.append(hashlib.sha256(raw).hexdigest())
        finally:
            os.close(fd)
    return result


@contextmanager
def _guard_state(database, *, writable=False):
    """Read bound completion history; only SCANNING still pins live target files."""
    from .target_execution_filesystem import _open_absolute
    path = _guard_path(database)
    parent = _open_absolute(path.parent)
    fd = None
    try:
        fd = os.open(path.name, (os.O_RDWR if writable else os.O_RDONLY)
            | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent)
        before = journal._check_file(fd, parent, path.name)
        if before.st_size > _MAX_GUARD_BYTES:
            raise _stop('guard too large')
        raw = os.pread(fd, _MAX_GUARD_BYTES + 1, 0)
        frames = raw.splitlines(keepends=True)
        if len(frames) not in (2, 3) or any(not frame.endswith(b'\n') for frame in frames):
            raise _stop('persistent execution guard has no explicit scanning transition')
        bodies = []
        for frame in frames:
            value = journal._strict_json_load(frame.decode())
            digest = value.pop('sha256')
            if journal._digest(value) != digest:
                raise _stop('guard checksum differs')
            bodies.append(value)
        original, completed = bodies[:2]
        if set(completed) != {'state', 'manifest', 'targets'} or completed['state'] != 'SCANNING':
            raise _stop('unknown guard state')
        manifest = manifest_from_json(completed['manifest'])
        root = Path(manifest.payload.library_root)
        if (original['schema'] != 'animedb-v6-scratch-execution-guard'
                or original['version'] != 1 or original['database'] != str(database)
                or original['library'] != str(root) or _scratch_library_root(database) != root
                or original['database_identity'] != list(journal._identity(database.stat()))
                or original['library_identity'] != list(journal._identity(root.stat()))
                or original['manifest_id'] != manifest.manifest_id
                or original['manifest_sha256'] != hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest()
                or original['journal'] != str(root.parent / (root.name + '.journal.jsonl'))):
            raise _stop('completed guard binding differs')
        expected = tuple(JournalEntry(manifest.manifest_id, action.action_id, index, state)
            for index, action in enumerate(manifest.payload.actions)
            for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'))
        if journal.load_journal(Path(original['journal']), manifest).entries != expected:
            raise _stop('completed guard journal differs')
        if (len(completed['targets']) != len(manifest.payload.actions)
                or [p[0] for p in completed['targets']] != [a.target for a in manifest.payload.actions]):
            raise _stop('completed target identities differ')
        state = manifest
        if len(bodies) == 3:
            normal = bodies[2]
            if (set(normal) != {'state', 'scanning_sha256', 'identities'}
                    or normal['state'] != 'NORMAL'
                    or normal['scanning_sha256'] != hashlib.sha256(b''.join(frames[:2])).hexdigest()
                    or normal['identities'] != _identity_history(manifest, parent)):
                raise _stop('normal-operation completion evidence differs')
            state = NormalOperation(manifest.manifest_id)
        for action, pin in (() if isinstance(state, NormalOperation) else zip(manifest.payload.actions, completed['targets'])):
            # Exact target/inode binding, never a name/size/content matching heuristic.
            target = root / action.target
            directory = _open_absolute(target.parent)
            try:
                info = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
            finally:
                os.close(directory)
            if not stat.S_ISREG(info.st_mode) or list(journal._identity(info)) != pin[1:]:
                raise _stop('known execution target missing or replaced')
            if action.action != 'KEEP' and _present(root / action.source):
                raise _stop('execution source and target conflict')
        after = journal._check_file(fd, parent, path.name)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise _stop('guard changed during admission')
        yield state, fd, parent, raw
    except ExecutionGuardStopped:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise _stop('completed execution guard unconfirmed') from exc
    finally:
        journal._close_fds((fd, parent))


def complete_reorganization(engine, root):
    """Explicitly retire verified SCANNING into normal operation on scratch data.

    Uses the existing exclusive maintenance boundary and appends to the same
    guard. It never resumes a move, rewrites a journal or changes domain rows.
    """
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session
    from .scanner.execution_rescan import verify_execution_rescan
    if not isinstance(engine, Engine):
        raise _stop('completion requires an engine, without a caller-owned transaction')
    database = _database_path(engine)
    if database is None or _scratch_library_root(database) != _library_path(root):
        raise _stop('completion requires the original scratch database/library binding')
    gate = maintenance_for(engine) or ScratchMaintenance(database, _library_path(root))
    if gate.database_path != database or gate.library_root != _library_path(root):
        raise _stop('completion maintenance binding differs')
    attempted = False
    guard_identity = None
    try:
        with gate.maintenance() as lease:
            with _guard_state(database, writable=True) as (state, fd, parent, raw):
                if isinstance(state, NormalOperation):
                    return
                with lease.write_scope(), Session(engine) as session:
                    try:
                        verify_execution_rescan(session, _library_path(root), state)
                    except ValueError as exc:
                        raise _stop(f'completion rescan differs: {exc}') from exc
                identities = _identity_history(state, parent)
                # Repeat physical/DB-bound guard checks after reading all evidence.
                with _guard_state(database) as (checked, _, _, current):
                    if checked != state or current != raw:
                        raise _stop('execution guard changed during completion')
                gate._check_binding(lease)
                body = {'state': 'NORMAL', 'scanning_sha256': hashlib.sha256(raw).hexdigest(),
                    'identities': identities}
                frame = (journal._canonical({**body, 'sha256': journal._digest(body)}) + '\n').encode()
                if len(raw) + len(frame) > _MAX_GUARD_BYTES:
                    raise _stop('completed guard exceeds supported size')
                guard_identity = journal._identity(os.fstat(fd))
                attempted = True
                os.lseek(fd, 0, os.SEEK_END)
                journal._write_all(fd, frame)
                os.fsync(fd)
                os.fsync(parent)
                if os.pread(fd, _MAX_GUARD_BYTES + 1, 0) != raw + frame:
                    raise _stop('completion append changed')
                journal._check_parent(_guard_path(database), parent)
                journal._check_file(fd, parent, _guard_path(database).name)
                gate._check_binding(lease)
    except BaseException as exc:
        if attempted:
            _revoke_transition(_guard_path(database), guard_identity, exc)
        if isinstance(exc, (MaintenanceStopped, KeyboardInterrupt, SystemExit)):
            raise
        raise _stop('normal-operation completion unconfirmed') from exc


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
def ordinary_scan_admission(bind, root, *, allow_completed_scan=False):
    """Check before SQL/autoflush/probe; retain cooperating exclusion to commit.

    Discovery of an existing scratch boundary works in a new plain engine too.
    This does not activate a production executor or protect noncooperating SQL.
    """
    with ExitStack() as admissions:
        try:
            root = _library_path(root)
            database = _database_path(bind)
            if database is not None:
                manifest = _require_no_evidence(database)
            else:
                manifest = None
            if manifest is not None and not isinstance(manifest, NormalOperation) and not allow_completed_scan:
                raise _stop('completed guard permits only explicit scanner routing, not startup writes')
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
                manifest = _require_no_evidence(database)
            if manifest is not None and not isinstance(manifest, NormalOperation) and not allow_completed_scan:
                raise _stop('completed guard permits only explicit scanner routing, not startup writes')
        except MaintenanceStopped:
            raise
        except (OSError, ValueError) as exc:
            raise _stop('execution guard admission could not be verified') from exc
        yield manifest
