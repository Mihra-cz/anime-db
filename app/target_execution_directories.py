"""Private E7F preparation inside the fresh E7A owner, never a resume API."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import replace
import hashlib
import os

from . import target_execution_filesystem as fs
from . import target_execution_journal as journal
from .target_execution_coordination import ScratchFilesystemReconciliationRequired
from .target_execution_manifest import _canonical, _digest, manifest_to_json
from .target_planner_filesystem import inventory_filesystem
from .target_planner_types import FilesystemEntry
from .target_planner import relative_locator_safe


class _DirectoryPreparation:
    def __init__(self, owner, snapshot):
        self.owner, self.filesystem = owner, owner._filesystem
        self.manifest, self._expected_header = owner._manifest, snapshot
        self._expected_entries = {entry.relative_path: entry for entry in snapshot.entries}
        self._preparing = ()
        self._approved_directories = frozenset(self.manifest.payload.directories)
        self.root_mtime_ns = os.fstat(self.filesystem._root_fd).st_mtime_ns
        self.pins = {}
        self.protected_pins = {}
        self._excluded_root_fds = {}
        self.created = set()
        self.removed = set()
        self.raw, self.previous, self.sequence = bytearray(), None, 0
        self._artifact_checkpoint = None
        self._last_raw, self._last_offset = b'', 0
        self.fd = self.parent_fd = None
        self.ready = False
        self._mkdir_attempted = False
        # Retain original identities, not one FD per directory. Every use
        # reopens via nofollow traversal and proves the same mount and identity.
        for entry in snapshot.entries:
            if entry.kind == 'directory':
                self._pin(entry.relative_path)
            elif entry.kind == 'system_excluded':
                self._pin(entry.relative_path, excluded=True)
                self.protected_pins[entry.relative_path] = self.pins.pop(entry.relative_path)
        self.check_snapshot()

    @property
    def expected(self):
        return replace(self._expected_header, entries=tuple(
            self._expected_entries[path] for path in sorted(self._expected_entries)))

    def _pin(self, path, *, excluded=False):
        parts = self._parts(path, excluded=excluded)
        with ExitStack() as stack:
            parent = self.filesystem._parent(parts, stack)
            flags = (os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC) if excluded else fs._DIRECTORY
            fd = os.open(parts[-1], flags, dir_fd=parent)
            stack.enter_context(fs._held_evidence_fd(fd))
            if not excluded:
                fs._secure_directory(fd)
            self.filesystem._check_fd(fd)
            named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if fs._identity(named) != fs._identity(os.fstat(fd)):
                raise fs.RenameRejected('STOP: directory entry changed during pin')
            if excluded and '/' not in path:
                # Opaque root namespaces cannot use ctime as a fence. Keep
                # their O_PATH inode pins (IMPORT/#recycle only), a constant
                # count independent of videos and normal directory count.
                pin = os.dup(fd)
                self.owner._resources.enter_context(fs._held_evidence_fd(pin))
                self._excluded_root_fds[path] = pin
            self.pins[path] = fs._identity(named)
            return named

    @staticmethod
    def _parts(path, *, excluded=False):
        # #recycle remains forbidden to every action. A directory-only O_PATH
        # observation may bind the inventory's excluded entry without descent.
        if excluded:
            if not relative_locator_safe(path) or '\x00' in path:
                raise fs.RenameRejected('STOP: unsafe excluded directory path')
            return tuple(path.split('/'))
        return fs._locator(path)

    @contextmanager
    def open(self, path):
        if path in {'', '.'}:
            self.filesystem._check_root()
            yield self.filesystem._root_fd
            self.filesystem._check_root()
            return
        expected = self.protected_pins[path] if path in self.protected_pins else self.pins[path]
        parts = self._parts(path, excluded=path in self.protected_pins)
        with ExitStack() as stack:
            parent = self.filesystem._parent(parts, stack)
            excluded = path in self.protected_pins
            flags = (os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC) if excluded else fs._DIRECTORY
            fd = os.open(parts[-1], flags, dir_fd=parent)
            stack.enter_context(fs._held_evidence_fd(fd))
            if not excluded:
                fs._secure_directory(fd)
            self.filesystem._check_fd(fd)
            if path in self._excluded_root_fds:
                pin = self._excluded_root_fds[path]
                self.filesystem._check_fd(pin)
                if fs._identity(os.fstat(pin)) != expected or os.fstat(pin).st_nlink == 0:
                    raise fs.RenameRejected('STOP: original excluded root directory lost')
            if (fs._identity(os.fstat(fd)) != expected
                    or fs._identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != expected):
                raise fs.RenameRejected('STOP: reopened directory identity changed')
            self.filesystem._parent_binding(parts, parent)
            yield fd
            if (path not in self.removed and
                    fs._identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != expected):
                raise fs.RenameRejected('STOP: reopened directory binding changed')
            if path not in self.removed:
                self.filesystem._parent_binding(parts, parent)

    def check_bindings(self, *, paths=None):
        self.filesystem._check_root()
        selected = self.pins.keys() | self.protected_pins.keys() if paths is None else (
            (self.pins.keys() & paths) | self.protected_pins.keys())
        for path in selected:
            if path in self.removed:
                # E7H already proved nlink=0 while the active rmdir FD was held.
                if (self.owner.root / path).exists() or (self.owner.root / path).is_symlink():
                    raise fs.RenameRejected('STOP: approved removed directory reappeared')
                continue
            with self.open(path):
                pass

    def check_snapshot(self):
        self.check_bindings()
        # Archive hashes were admitted with R0 and every source's full SHA.
        # Carry those digests across mkdir barriers, with exact source identity
        # and ctime fences in _check_initial; do not re-read archives per mkdir.
        snapshot = inventory_filesystem(self.owner.root, hash_archives=False)
        digests = {entry.relative_path: entry.content_sha256 for entry in self.expected.entries}
        snapshot = replace(snapshot, entries=tuple(replace(entry,
            content_sha256=digests.get(entry.relative_path)) for entry in snapshot.entries))
        if (snapshot != self.expected
                or os.fstat(self.filesystem._root_fd).st_mtime_ns != self.root_mtime_ns):
            raise fs.RenameRejected('STOP: filesystem differs from original R0 plus verified mkdirs')
        self.check_bindings()

    def target_absent(self, action):
        """A missing ancestor is allowed only in the original sealed set."""
        parts = fs._locator(action.target)
        with ExitStack() as stack:
            parent = self.filesystem._root_fd
            for index, part in enumerate(parts[:-1], 1):
                path = '/'.join(parts[:index])
                if path not in self._approved_directories:
                    raise fs.RenameRejected('STOP: unapproved target parent')
                try:
                    fd = os.open(part, fs._DIRECTORY, dir_fd=parent)
                except FileNotFoundError:
                    if self.ready or path in self.pins:
                        raise fs.RenameRejected('STOP: prepared target parent disappeared')
                    return
                stack.enter_context(fs._held_evidence_fd(fd))
                fs._secure_directory(fd)
                self.filesystem._check_fd(fd)
                parent = fd
            fs._absent(parent, parts[-1])

    def _check_artifact(self, *, full=False):
        self.owner._check_execution()
        journal._check_parent(self.owner.directory_evidence_path, self.parent_fd)
        journal._check_file(self.fd, self.parent_fd, self.owner.directory_evidence_path.name)
        self.owner._check_storage_fd(self.parent_fd)
        self.owner._check_storage_fd(self.fd)
        if (journal._checkpoint_stat(os.fstat(self.fd)) != self._artifact_checkpoint
                or os.pread(self.fd, len(self._last_raw) + 1, self._last_offset) != self._last_raw
                or (full and os.pread(self.fd, len(self.raw) + 1, 0) != self.raw)):
            raise fs.RenameRejected('STOP: directory preparation evidence changed')

    def _append(self, state, **values):
        self._check_artifact()
        body = dict(sequence=self.sequence, previous_sha256=self.previous, state=state, **values)
        digest = _digest(body)
        raw = (_canonical(dict(body, sha256=digest)) + '\n').encode('utf-8')
        journal._write_all(self.fd, raw)
        os.fsync(self.fd)
        os.fsync(self.parent_fd)
        self._last_offset, self._last_raw = len(self.raw), raw
        self._artifact_checkpoint = journal._checkpoint_stat(os.fstat(self.fd))
        self.raw += raw
        self._check_artifact()
        self.sequence += 1
        self.previous = digest

    def _create_evidence(self):
        path = self.owner.directory_evidence_path
        self.parent_fd = journal._open_parent(path, self.manifest, (), (self.owner.database_path,))
        self.owner._resources.enter_context(fs._held_evidence_fd(self.parent_fd))
        self.owner._check_storage_fd(self.parent_fd)
        self.fd = os.open(path.name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
            | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.parent_fd)
        self.owner._resources.enter_context(fs._held_evidence_fd(self.fd))
        journal._lock(self.fd, True)
        self._artifact_checkpoint = journal._checkpoint_stat(os.fstat(self.fd))
        self._append('BOUND', schema='animedb-v6-directory-preparation', version=1,
            manifest_id=self.manifest.manifest_id,
            manifest_sha256=hashlib.sha256(manifest_to_json(self.manifest).encode('utf-8')).hexdigest(),
            root=str(self.owner.root), root_identity=self.filesystem._root_identity,
            mount_identity=self.owner.mount_identity, mount_id=self.filesystem._mount_id,
            filesystem_hash=self.manifest.payload.filesystem_hash,
            directories=self.manifest.payload.directories,
            existing=self.pins)

    def _check_initial(self, *, full=False):
        self.owner._check_execution()
        if self.manifest is not self.owner._manifest:
            raise fs.RenameRejected('STOP: original sealed manifest changed')
        self.owner._database._check_stage(0)
        if full:
            self.check_snapshot()
            for index in self.owner._prepared:
                self.owner._verify_prepared(index)
        else:
            for path in self._preparing:
                with self.open(path) as fd:
                    expected = self._expected_entries.get(path)
                    mtime = expected.mtime_ns if expected is not None else self.root_mtime_ns
                    if os.fstat(fd).st_mtime_ns != mtime:
                        raise fs.RenameRejected('STOP: active mkdir directory changed')
        if self.fd is not None:
            self._check_artifact(full=full)

    def prepare(self):
        try:
            self._prepare()
        except Exception as exc:
            if not self._mkdir_attempted or isinstance(exc, ScratchFilesystemReconciliationRequired):
                raise
            # A rejected later check cannot prove that a preceding physical
            # attempt left the approved state intact. Preserve its cause/phase.
            raise ScratchFilesystemReconciliationRequired(
                'STOP: directory preparation unconfirmed: ' + str(exc),
                getattr(exc, 'phase', self.owner._phase)) from exc

    def _prepare(self):
        self.owner._phase = 'DIRECTORY_PREPARATION'
        self._check_initial(full=True)
        self._create_evidence()
        for path in self.manifest.payload.directories:
            from .target_planner import import_namespace
            if import_namespace(path):
                raise fs.RenameRejected('STOP: protected IMPORT cannot be a target parent')
            if path in self.pins:
                continue
            self._check_initial()
            parts = fs._locator(path)
            parent_path = '/'.join(parts[:-1])
            self._preparing = (parent_path,)
            with self.open(parent_path) as parent:
                fs._absent(parent, parts[-1])
                self._append('INTENT', path=path, parent_identity=fs._identity(os.fstat(parent)))
                self._check_initial()
                self._mkdir_attempted = True
                os.mkdir(parts[-1], 0o700, dir_fd=parent)
                self._pin(path)
                self._preparing = (parent_path, path)
                with self.open(path) as fd:
                    info, parent_info = os.fstat(fd), os.fstat(parent)
                    self._account_mkdir(path, parent_path, info, parent_info)
                    os.fsync(fd)
                    os.fsync(parent)
                    self._check_initial()
                    self._append('VERIFIED', path=path, identity=fs._identity(info),
                        mtime_ns=info.st_mtime_ns, parent_mtime_ns=parent_info.st_mtime_ns)
            self.created.add(path)
        self._preparing = ()
        self._check_initial(full=True)
        self._append('READY', created=tuple(path for path in self.manifest.payload.directories if path in self.created))
        self._check_initial(full=True)
        self.ready = True

    def _account_mkdir(self, path, parent_path, info, parent_info):
        # Account only for this new entry and its direct parent's mtime.
        # All other paths/kinds/file stats/directory mtimes stay exact R0
        # evidence. Never normalize or re-seal the original FS baseline.
        if not parent_path:
            self.root_mtime_ns = parent_info.st_mtime_ns
        if parent_path:
            self._expected_entries[parent_path] = replace(
                self._expected_entries[parent_path], mtime_ns=parent_info.st_mtime_ns)
        self._expected_entries[path] = FilesystemEntry(path, 'directory', mtime_ns=info.st_mtime_ns)
