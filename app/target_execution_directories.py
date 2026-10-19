"""Private E7F preparation inside the fresh E7A owner, never a resume API."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import os

from . import target_execution_filesystem as fs
from . import target_execution_journal as journal
from .target_execution_coordination import ScratchFilesystemReconciliationRequired
from .target_execution_manifest import _canonical, _digest, manifest_to_json
from .target_planner_filesystem import inventory_filesystem
from .target_planner_types import FilesystemEntry


class _DirectoryPreparation:
    def __init__(self, owner, snapshot):
        self.owner, self.filesystem = owner, owner._filesystem
        self.manifest, self.expected = owner._manifest, snapshot
        self.root_mtime_ns = os.fstat(self.filesystem._root_fd).st_mtime_ns
        self.pins = {}
        self.created = set()
        self.raw, self.previous, self.sequence = b'', None, 0
        self.fd = self.parent_fd = None
        self.ready = False
        self._mkdir_attempted = False
        # Hold every original directory, including source-only parents. Snapshot
        # equality alone cannot detect replacement with restored timestamps.
        for entry in snapshot.entries:
            if entry.kind == 'directory':
                self._pin(entry.relative_path)
        self.check_snapshot()

    def _pin(self, path):
        parts = fs._locator(path)
        with ExitStack() as stack:
            parent = self.filesystem._parent(parts, stack)
            fd = os.open(parts[-1], fs._DIRECTORY, dir_fd=parent)
            self.owner._resources.enter_context(fs._held_evidence_fd(fd))
            fs._secure_directory(fd)
            self.filesystem._check_fd(fd)
            named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if fs._identity(named) != fs._identity(os.fstat(fd)):
                raise fs.RenameRejected('STOP: directory entry changed during pin')
            self.pins[path] = fd
            return fd

    def check_bindings(self):
        self.filesystem._check_root()
        for path, fd in self.pins.items():
            self.filesystem._parent_binding(fs._locator(path) + ('<child>',), fd)

    def check_snapshot(self):
        self.check_bindings()
        if (inventory_filesystem(self.owner.root) != self.expected
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
                if path not in self.manifest.payload.directories:
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

    def _check_artifact(self):
        self.owner._check_execution()
        journal._check_parent(self.owner.directory_evidence_path, self.parent_fd)
        journal._check_file(self.fd, self.parent_fd, self.owner.directory_evidence_path.name)
        self.filesystem._check_fd(self.parent_fd)
        self.filesystem._check_fd(self.fd)
        if os.pread(self.fd, len(self.raw) + 1, 0) != self.raw:
            raise fs.RenameRejected('STOP: directory preparation evidence changed')

    def _append(self, state, **values):
        self._check_artifact()
        body = dict(sequence=self.sequence, previous_sha256=self.previous, state=state, **values)
        digest = _digest(body)
        raw = (_canonical(dict(body, sha256=digest)) + '\n').encode('utf-8')
        journal._write_all(self.fd, raw)
        os.fsync(self.fd)
        os.fsync(self.parent_fd)
        self.raw += raw
        self._check_artifact()
        self.sequence += 1
        self.previous = digest

    def _create_evidence(self):
        path = self.owner.directory_evidence_path
        self.parent_fd = journal._open_parent(path, self.manifest, (), (self.owner.database_path,))
        self.owner._resources.enter_context(fs._held_evidence_fd(self.parent_fd))
        self.filesystem._check_fd(self.parent_fd)
        self.fd = os.open(path.name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
            | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.parent_fd)
        self.owner._resources.enter_context(fs._held_evidence_fd(self.fd))
        journal._lock(self.fd, True)
        self._append('BOUND', schema='animedb-v6-directory-preparation', version=1,
            manifest_id=self.manifest.manifest_id,
            manifest_sha256=hashlib.sha256(manifest_to_json(self.manifest).encode('utf-8')).hexdigest(),
            root=str(self.owner.root), root_identity=self.filesystem._root_identity,
            mount_identity=self.owner.mount_identity, mount_id=self.filesystem._mount_id,
            filesystem_hash=self.manifest.payload.filesystem_hash,
            directories=self.manifest.payload.directories,
            existing={path: fs._identity(os.fstat(fd)) for path, fd in self.pins.items()})

    def _check_initial(self):
        self.owner._check_execution()
        if manifest_to_json(self.manifest) != self.owner._manifest_bytes:
            raise fs.RenameRejected('STOP: original sealed manifest changed')
        self.owner._database._check_stage(0)
        self.check_snapshot()
        for index in self.owner._prepared:
            self.owner._verify_prepared(index)
        if self.fd is not None:
            self._check_artifact()

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
        self._check_initial()
        self._create_evidence()
        for path in self.manifest.payload.directories:
            if path in self.pins:
                continue
            self._check_initial()
            parts = fs._locator(path)
            parent_path = '/'.join(parts[:-1])
            parent = self.pins[parent_path] if parent_path else self.filesystem._root_fd
            fs._absent(parent, parts[-1])
            self._append('INTENT', path=path, parent_identity=fs._identity(os.fstat(parent)))
            # An event/fsync callback or a cooperating owner loss must not open
            # a gap between preflight and the first physical operation.
            self._check_initial()
            self._mkdir_attempted = True
            os.mkdir(parts[-1], 0o700, dir_fd=parent)
            fd = self._pin(path)
            # Account only for this new entry and its direct parent's mtime.
            # All other paths/kinds/file stats/directory mtimes stay exact R0
            # evidence. Never normalize or re-seal the original FS baseline.
            info, parent_info = os.fstat(fd), os.fstat(parent)
            if not parent_path:
                self.root_mtime_ns = parent_info.st_mtime_ns
            entries = tuple(replace(e, mtime_ns=parent_info.st_mtime_ns)
                if e.relative_path == parent_path else e for e in self.expected.entries)
            self.expected = replace(self.expected, entries=tuple(sorted(entries + (
                FilesystemEntry(path, 'directory', mtime_ns=info.st_mtime_ns),), key=lambda e: e.relative_path)))
            # Pin the syscall's delta before barriers; do not absorb an alien
            # timestamp change observed later as an effect of our mkdir.
            os.fsync(fd)
            os.fsync(parent)
            self._check_initial()
            self._append('VERIFIED', path=path, identity=fs._identity(info),
                mtime_ns=info.st_mtime_ns, parent_mtime_ns=parent_info.st_mtime_ns)
            self.created.add(path)
        self._check_initial()
        self._append('READY', created=tuple(path for path in self.manifest.payload.directories if path in self.created))
        self._check_initial()
        self.ready = True
