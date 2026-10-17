"""E5A private, single-video SQLite transaction for a fresh scratch coordinator.

Only the coordinator calls this lane, while retaining its live identity pins and
sole E1 writer. No schema/startup, arbitrary DB path, restart or retry support.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
import os
import sqlite3
from urllib.parse import quote

from . import target_execution_filesystem as fs
from . import target_execution_identity as identity
from . import target_execution_journal as journal
from .database import Base
from .models import Video
from .target_execution_manifest import DatabaseFingerprint, LocatorPatch, post_state_hash_from_manifest
from .target_planner_filesystem import inventory_filesystem
from .target_planner_service import load_planner_context, readonly_planner_session


def _rows(connection):
    # Fixed application tables, with every persisted column and stable PK order.
    return {table.name: tuple(connection.execute(
        'SELECT ' + ','.join('"' + c.name + '"' for c in table.columns) +
        ' FROM "' + table.name + '" ORDER BY ' +
        ','.join('"' + c.name + '"' for c in table.primary_key.columns)).fetchall())
        for table in Base.metadata.sorted_tables}


def _schema(connection):
    return (tuple(connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')),
        connection.execute('PRAGMA user_version').fetchone()[0])


class _ScratchVideoDatabase:
    def __init__(self, filesystem, path, manifest, action):
        self._resources = ExitStack()
        self._filesystem, self.path = filesystem, path
        self._manifest, self._action = manifest, action
        self._applied = self._commit_attempted = self._committed = self._closed = False
        try:
            if (action.action != 'MOVE' or action.object_kind != 'video'
                    or type(action.object_id) is not int or action.object_id <= 0
                    or manifest.payload.expected_post_hash is None
                    or path != filesystem.root.parent / (filesystem.root.name + '.sqlite')):
                raise ValueError('one exact video MOVE, post-state hash and adjacent scratch SQLite required')
            # Reject unrelated/dependent patches before opening any DB connection.
            if any(p.object_kind != 'video' or p.object_id != action.object_id
                    or p.field not in {'relative_path', 'root_folder'} for p in manifest.payload.locator_patches):
                raise ValueError('locator patches exceed this single-video scope')
            self._open()
            fields = list(Video.__table__.columns.keys())
            id_index = fields.index('id')
            selected = [row for row in self._before['videos'] if row[id_index] == action.object_id]
            if len(selected) != 1:
                raise ValueError('expected Video.id is missing or ambiguous')
            values = dict(zip(fields, selected[0]))
            if (values['relative_path'] != action.source
                    or (values['size'], values['mtime_ns']) != (action.expected_size, action.expected_mtime_ns)):
                raise ValueError('original Video source/size/mtime precondition mismatch')
            self.relative_path, self.root_folder = action.target, action.target.split('/')[0]
            self._old_root = values['root_folder']
            patches = tuple(LocatorPatch('video', action.object_id, name, values[name], after)
                for name, after in (('relative_path', self.relative_path), ('root_folder', self.root_folder))
                if values[name] != after)
            if (len(manifest.payload.locator_patches) != len(patches)
                    or set(manifest.payload.locator_patches) != set(patches)):
                raise ValueError('exact original/target locator patches required; no omissions or extra patches')
            if any(row[id_index] != action.object_id and row[fields.index('relative_path')] == action.target
                    for row in self._before['videos']):
                raise ValueError('target locator belongs to another Video')
            expected = []
            for row in self._before['videos']:
                current = list(row)
                if row[id_index] == action.object_id:
                    current[fields.index('relative_path')] = self.relative_path
                    current[fields.index('root_folder')] = self.root_folder
                expected.append(tuple(current))
            self._expected = {**self._before, 'videos': tuple(expected)}
            self._check_before()
        except BaseException as exc:
            self._resources.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def _open(self):
        self._filesystem._check_root()
        parent = fs._open_absolute(self.path.parent)
        self._resources.enter_context(fs._held_evidence_fd(parent))
        self._parent = parent
        fs._check_mounts(self.path.parent, parent, self._filesystem._mounts)
        fd = os.open(self.path.name, fs._FILE, dir_fd=parent)
        self._resources.enter_context(fs._held_evidence_fd(fd))
        self._fd = fd
        self._check_path()
        identity._safe_file(identity._file_stat(os.fstat(fd)))
        # Refuse WAL/unknown headers before SQLite can open sidecar files.
        # https://sqlite.org/fileformat.html#file_format_version_numbers
        header = os.pread(fd, 100, 0)
        if (len(header) != 100 or header[:16] != b'SQLite format 3\0'
                or header[18:20] != b'\x01\x01'):
            raise ValueError('existing rollback-mode SQLite header required; no WAL or initialization')
        for suffix in ('-wal', '-shm', '-journal'):
            fs._absent(parent, self.path.name + suffix)
        self._uri = 'file:' + quote(str(self.path), safe='/')
        connection = sqlite3.connect(self._uri + '?mode=rw', uri=True, timeout=0, isolation_level=None)
        self._resources.enter_context(closing(connection))
        self._connection = connection
        if connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            raise ValueError('scratch SQLite requires DELETE journal mode, without WAL or hot journal')
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        self._version = connection.execute('PRAGMA data_version').fetchone()[0]
        if self._fingerprint() != self._manifest.payload.db_fingerprint:
            raise ValueError('SQLite fingerprint differs from approved manifest')
        self._before = _rows(connection)
        self._schema = _schema(connection)

    def _check_path(self):
        filesystem = self._filesystem
        filesystem._check_root()
        journal._check_parent(self.path, self._parent)
        info = journal._check_file(self._fd, self._parent, self.path.name)
        fs._check_mounts(self.path.parent, self._parent, filesystem._mounts)
        if info.st_dev != filesystem._root_identity[0] or fs._mount_id(self._fd) != filesystem._mount_id:
            raise ValueError('scratch SQLite root/mount binding changed')

    def _fingerprint(self):
        self._check_path()
        observed, content = identity._hash_file(self._fd)
        version = self._connection.execute('PRAGMA user_version').fetchone()[0]
        self._check_path()
        return DatabaseFingerprint(content.digest, observed.size, observed.mtime_ns, version)

    def _check_before(self):
        if (self._applied or self._closed or self._connection.total_changes != 0
                or not self._connection.in_transaction
                or self._connection.execute('PRAGMA data_version').fetchone()[0] != self._version
                or self._fingerprint() != self._manifest.payload.db_fingerprint
                or _rows(self._connection) != self._before or _schema(self._connection) != self._schema):
            raise ValueError('SQLite changed before locator update')

    def _apply(self):
        self._check_before()
        self._applied = True  # A failed DML is spent too, never retried.
        cursor = self._connection.execute(
            'UPDATE videos SET relative_path=?,root_folder=? WHERE id=? AND relative_path=? AND root_folder=?',
            (self.relative_path, self.root_folder, self._action.object_id, self._action.source, self._old_root))
        if (cursor.rowcount != 1 or self._connection.total_changes != 1
                or _rows(self._connection) != self._expected or _schema(self._connection) != self._schema):
            raise ValueError('DB update conflict or write outside the approved locator patches')
        self._check_path()

    def _commit(self):
        if not self._applied or self._commit_attempted or self._closed:
            raise ValueError('SQLite commit unavailable; no retry')
        self._commit_attempted = True
        self._connection.commit()
        if self._connection.in_transaction:
            raise ValueError('SQLite commit not acknowledged')
        self._committed = True
        # Reacquire the writer reservation for verification/journal acknowledgement.
        # No second DML or commit: close aborts this empty reservation transaction.
        self._connection.execute('BEGIN IMMEDIATE')

    def _verify_stored(self):
        if (not self._committed or self._closed or not self._connection.in_transaction
                or self._connection.execute('PRAGMA data_version').fetchone()[0] != self._version):
            raise ValueError('no stable acknowledged SQLite commit')
        self._check_path()
        # An independent read-only connection proves persistence outside the writer.
        with closing(sqlite3.connect(self._uri + '?mode=ro', uri=True, timeout=0)) as fresh:
            fresh.execute('BEGIN')
            if _rows(fresh) != self._expected or _schema(fresh) != self._schema:
                raise ValueError('stored DB state differs from the approved two-field projection')
        self._verify_post_hash()
        self._check_path()

    def _verify_post_hash(self):
        with readonly_planner_session(self.path) as session:
            context = load_planner_context(session)
        snapshot = inventory_filesystem(self._filesystem.root)
        if post_state_hash_from_manifest(self._manifest, context, snapshot) != self._manifest.payload.expected_post_hash:
            raise ValueError('fresh DB loader/filesystem post-state hash mismatch')
        self._check_path()

    def close(self):
        if not self._closed:
            self._closed = True
            # Closing aborts any uncommitted DB transaction; never undoes a commit
            # or performs a physical rollback, evidence repair or retry.
            self._resources.close()


class _ScratchPairDatabase(_ScratchVideoDatabase):
    """Exactly R0 -> R1 -> R2 on one held SQLite; never re-baseline after A."""

    def __init__(self, filesystem, path, manifest):
        self._resources = ExitStack()
        self._filesystem, self.path, self._manifest = filesystem, path, manifest
        self._closed = False
        self._applied_indices, self._commit_attempts = set(), set()
        self._committed_count = 0
        try:
            actions = manifest.payload.actions
            if (len(actions) != 2 or any(a.action != 'MOVE' or a.object_kind != 'video'
                    or a.criticality != 'PRIMARY' or type(a.object_id) is not int or a.object_id <= 0 for a in actions)
                    or actions[0].object_id == actions[1].object_id or actions[0].depends_on
                    or actions[1].depends_on != (actions[0].action_id,)
                    or manifest.payload.expected_post_hash is None
                    or path != filesystem.root.parent / (filesystem.root.name + '.sqlite')):
                raise ValueError('exactly two dependent PRIMARY Video MOVE actions and adjacent SQLite required')
            if any(p.object_kind != 'video' or p.object_id not in {a.object_id for a in actions}
                    or p.field not in {'relative_path', 'root_folder'} for p in manifest.payload.locator_patches):
                raise ValueError('patches exceed the exact two-video scope')
            self._open()
            if {row[1] for row in self._schema[0] if row[0] == 'table'} != set(Base.metadata.tables):
                raise ValueError('only the complete known scratch application schema is supported')
            columns = list(Video.__table__.columns.keys())
            self._columns = columns
            self._originals = []
            states, patches = [self._before], []
            for action in actions:
                selected = [dict(zip(columns, row)) for row in self._before['videos']
                    if row[columns.index('id')] == action.object_id]
                if len(selected) != 1:
                    raise ValueError('one original row per Video required')
                values = selected[0]
                if (values['relative_path'] != action.source
                        or (values['size'], values['mtime_ns']) != (action.expected_size, action.expected_mtime_ns)):
                    raise ValueError('original Video source/size/mtime precondition mismatch')
                if any(row[columns.index('id')] != action.object_id
                        and row[columns.index('relative_path')] == action.target for row in self._before['videos']):
                    raise ValueError('target locator belongs to another Video')
                self._originals.append(values)
                after = {'relative_path': action.target, 'root_folder': action.target.split('/')[0]}
                patches.extend(LocatorPatch('video', action.object_id, field, values[field], value)
                    for field, value in after.items() if values[field] != value)
                projected = []
                for row in states[-1]['videos']:
                    current = list(row)
                    if row[columns.index('id')] == action.object_id:
                        for field, value in after.items():
                            current[columns.index(field)] = value
                    projected.append(tuple(current))
                states.append({**states[-1], 'videos': tuple(projected)})
            if (len(patches) != len(manifest.payload.locator_patches)
                    or set(patches) != set(manifest.payload.locator_patches)):
                raise ValueError('exact complete R0 locator patches for both Videos required')
            self._states = tuple(states)
            self._ack_fingerprint = manifest.payload.db_fingerprint
            self._check_stage(0)
        except BaseException as exc:
            self._resources.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def _check_stage(self, index):
        if (type(index) is not int or index not in (0, 1) or self._closed
                or self._committed_count != index or index in self._applied_indices
                or not self._connection.in_transaction or self._connection.total_changes != index
                or self._connection.execute('PRAGMA data_version').fetchone()[0] != self._version
                or self._fingerprint() != self._ack_fingerprint
                or _rows(self._connection) != self._states[index] or _schema(self._connection) != self._schema):
            raise ValueError('SQLite differs from the original sealed R0/R1 projection')

    def _apply(self, index):
        self._check_stage(index)
        self._applied_indices.add(index)
        action = self._manifest.payload.actions[index]
        cursor = self._connection.execute(
            'UPDATE videos SET relative_path=?,root_folder=? WHERE id=? AND relative_path=? AND root_folder=?',
            (action.target, action.target.split('/')[0], action.object_id,
                action.source, self._originals[index]['root_folder']))
        if (cursor.rowcount != 1 or self._connection.total_changes != index + 1
                or _rows(self._connection) != self._states[index + 1]
                or _schema(self._connection) != self._schema):
            raise ValueError('pair locator update conflict or unapproved side effect')
        self._check_path()

    def _commit(self, index):
        if (self._closed or index not in self._applied_indices or index in self._commit_attempts
                or self._committed_count != index):
            raise ValueError('pair commit unavailable; no retry')
        self._commit_attempts.add(index)
        self._connection.commit()
        if self._connection.in_transaction:
            raise ValueError('pair commit not acknowledged')
        self._connection.execute('BEGIN IMMEDIATE')
        self._committed_count += 1
        self._ack_fingerprint = self._fingerprint()

    def _verify_stored(self, index):
        if (type(index) is not int or index not in (0, 1) or self._closed
                or self._committed_count != index + 1 or not self._connection.in_transaction
                or self._connection.total_changes != index + 1
                or self._connection.execute('PRAGMA data_version').fetchone()[0] != self._version
                or self._fingerprint() != self._ack_fingerprint
                or _rows(self._connection) != self._states[index + 1] or _schema(self._connection) != self._schema):
            raise ValueError('no unchanged acknowledged pair commit')
        with closing(sqlite3.connect(self._uri + '?mode=ro', uri=True, timeout=0)) as fresh:
            fresh.execute('BEGIN')
            if _rows(fresh) != self._states[index + 1] or _schema(fresh) != self._schema:
                raise ValueError('stored rows/schema differ from sealed R1/R2 projection')
        # R1 deliberately has no final post-state hash: B is still at its source.
        if index == 1:
            self._verify_post_hash()
        self._check_path()
