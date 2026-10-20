"""E7H: separately approved rmdir in the still-live, completed scratch owner.

No root adoption, file deletion, retry, resume or E1/manifest mutation. The
original post hash gates admission only; cleanup has its own snapshot contract.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass, replace
import hashlib
import os
import sqlite3
import stat
import threading

from . import target_execution_filesystem as fs
from . import target_execution_journal as journal
from .target_execution_coordination import ScratchFilesystemReconciliationRequired
from .target_execution_database import _rows, _schema
from .target_execution_identity import _hash_file
from .target_execution_manifest import JournalEntry, _canonical, _digest, manifest_to_json
from .target_execution_rollback import _owner as _require_maintenance
from .target_planner import technical_root
from .target_planner_coverage import DirectoryCoverage


@dataclass(frozen=True)
class CleanupEntry:
    path: str
    kind: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    content_sha256: str | None = None


def _entry(path, kind, info, content=None):
    return CleanupEntry(path, kind, info.st_dev, info.st_ino, info.st_mode,
        info.st_uid, info.st_gid, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns, content)


@dataclass(frozen=True)
class CleanupCandidate:
    path: str
    identity: tuple[int, int]
    disposition: str


@dataclass(frozen=True)
class CleanupProposal:
    manifest_id: str
    manifest_sha256: str
    execution_journal: str
    execution_identity: tuple[int, int]
    execution_sha256: str
    root: str
    root_identity: tuple[int, int]
    mount_identity: str
    mount_id: int
    original_filesystem_hash: str
    expected_post_hash: str
    database_sha256: str
    before_snapshot: tuple[CleanupEntry, ...]
    directory_review: tuple[DirectoryCoverage, ...]
    candidates: tuple[CleanupCandidate, ...]
    proposal_sha256: str


@dataclass(frozen=True)
class CleanupApproval:
    proposal_sha256: str
    directories: tuple[str, ...]
    actor: str
    reason: str
    approval_sha256: str


def _seal(value, field):
    body = asdict(value)
    body.pop(field)
    return _digest(body)


def approve_cleanup(proposal: CleanupProposal, directories: tuple[str, ...], *, actor: str, reason: str):
    """Explicit caller decision over this exact verified proposal; no I/O."""
    if (type(proposal) is not CleanupProposal
            or proposal.proposal_sha256 != _seal(proposal, 'proposal_sha256')
            or type(directories) is not tuple or not directories
            or any(type(p) is not str for p in directories)
            or len(set(directories)) != len(directories)
            or not set(directories) <= {d.path for d in proposal.candidates}
            or type(actor) is not str or not actor.strip() or type(reason) is not str or not reason.strip()):
        raise ValueError('explicit exact candidate paths, actor and reason required')
    # Approving a parent never implicitly approves its empty child directories.
    for path in directories:
        if any(e.path.startswith(path + '/') and
                (e.kind != 'directory' or e.path not in directories) for e in proposal.before_snapshot):
            raise ValueError('approved directory has retained/unknown contents')
    approval = CleanupApproval(proposal.proposal_sha256, directories, actor, reason, '')
    return replace(approval, approval_sha256=_seal(approval, 'approval_sha256'))


@dataclass(frozen=True)
class CleanupOutcome:
    manifest_id: str
    proposal_sha256: str
    removed: tuple[str, ...]
    preserved: tuple[str, ...]
    before_snapshot: tuple[CleanupEntry, ...]
    after_snapshot: tuple[CleanupEntry, ...]
    after_snapshot_sha256: str


class _Cleanup:
    def __init__(self, owner):
        self.owner, self.filesystem = owner, owner._filesystem
        self.manifest = owner._manifest
        self.pins = {'.': self.filesystem._root_fd, **owner._directories.pins}
        self.removed = []
        self.raw, self.previous, self.sequence = b'', None, 0
        self.fd = self.parent_fd = None
        self.expected = None
        self.journal_bytes = os.pread(owner._writer._fd, os.fstat(owner._writer._fd).st_size + 1, 0)
        self.database_stat, self.database_content = _hash_file(owner._database._fd)

    def _check_static(self):
        owner, filesystem, db = self.owner, self.filesystem, self.owner._database
        if (owner._closed or os.getpid() != owner._pid or not owner._mutex.locked()
                or threading.get_ident() != owner._execution_thread
                or manifest_to_json(self.manifest) != owner._manifest_bytes):
            raise fs.RenameRejected('STOP: cleanup lost its live execution owner')
        _require_maintenance(owner._maintenance_owner, owner.root)
        filesystem._check_root()
        expected = tuple(JournalEntry(self.manifest.manifest_id, action.action_id, index, state)
            for index, action in enumerate(self.manifest.payload.actions)
            for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'))
        if (owner._require_journal() != expected
                or os.pread(owner._writer._fd, len(self.journal_bytes) + 1, 0) != self.journal_bytes):
            raise fs.RenameRejected('STOP: complete original execution journal required')
        # Do not call E7D's original post-hash verification after rmdir. Check
        # the same held DB, full final Rn rows/schema and bytes without writes.
        journal._check_parent(db.path, db._parent)
        journal._check_file(db._fd, db._parent, db.path.name)
        filesystem._check_fd(db._fd)
        filesystem._check_fd(db._parent)
        if (_hash_file(db._fd) != (self.database_stat, self.database_content)
                or not db._connection.in_transaction
                or db._connection.total_changes != db._change_counts[-1]
                or db._connection.execute('PRAGMA data_version').fetchone()[0] != db._version):
            raise fs.RenameRejected('STOP: final DB projection changed during cleanup')
        with closing(sqlite3.connect(db._uri + '?mode=ro', uri=True, timeout=0)) as fresh:
            fresh.execute('PRAGMA query_only=ON')
            fresh.execute('BEGIN')
            if _rows(fresh) != db._states[-1] or _schema(fresh) != db._schema:
                raise fs.RenameRejected('STOP: independent final DB projection differs')
        preparation = owner._directories
        journal._check_parent(owner.directory_evidence_path, preparation.parent_fd)
        journal._check_file(preparation.fd, preparation.parent_fd, owner.directory_evidence_path.name)
        if os.pread(preparation.fd, len(preparation.raw) + 1, 0) != preparation.raw:
            raise fs.RenameRejected('STOP: original directory evidence changed')

    def _snapshot(self):
        """Read only through held nofollow directories; reject any unaccounted entry."""
        filesystem = self.filesystem
        filesystem._check_root()
        removed = set(self.removed)
        pins = {p: fd for p, fd in self.pins.items() if p not in removed}
        targets = {a.target: (i, a) for i, a in enumerate(self.manifest.payload.actions)}
        excluded = {e.relative_path for e in self.owner._database._initial_snapshot.entries if e.kind == 'system_excluded'}
        entries = {'.': _entry('.', 'directory', os.fstat(pins['.']))}
        for path, parent in sorted(pins.items()):
            filesystem._check_root()
            if path != '.':
                filesystem._parent_binding(fs._locator(path) + ('<child>',), parent)
            before = _entry(path, 'directory', os.fstat(parent))
            for name in sorted(os.listdir(parent)):
                child = name if path == '.' else path + '/' + name
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if child in excluded and stat.S_ISDIR(info.st_mode):
                    entries[child] = _entry(child, 'system_excluded', info)
                elif child in pins and child != '.' and stat.S_ISDIR(info.st_mode):
                    fd = pins[child]
                    filesystem._check_fd(fd)
                    fs._secure_directory(fd)
                    if fs._identity(info) != fs._identity(os.fstat(fd)):
                        raise fs.RenameRejected('STOP: cleanup directory identity changed')
                    entries[child] = _entry(child, 'directory', info)
                elif child in targets and stat.S_ISREG(info.st_mode):
                    index, action = targets[child]
                    fd, original, _, content = self.owner._prepared[index]
                    filesystem._check_fd(fd)
                    fs._check_source(fd, parent, name, action, original)
                    observed, hashed = _hash_file(fd)
                    if hashed != content:
                        raise fs.RenameRejected('STOP: completed file content changed')
                    current = fs._check_source(fd, parent, name, action, original)
                    if (observed.size, observed.mtime_ns, observed.ctime_ns) != (
                            current.st_size, current.st_mtime_ns, current.st_ctime_ns):
                        raise fs.RenameRejected('STOP: file changed during cleanup inventory')
                    entries[child] = _entry(child, 'regular', current, hashed.digest)
                else:
                    raise fs.RenameRejected('STOP: unknown content, symlink or unexpected directory: ' + child)
            if before != _entry(path, 'directory', os.fstat(parent)):
                raise fs.RenameRejected('STOP: directory changed during cleanup inventory')
        if set(entries) != set(pins) | set(targets) | excluded:
            raise fs.RenameRejected('STOP: cleanup inventory has missing entries')
        filesystem._check_root()
        return tuple(entries[p] for p in sorted(entries))

    def _check(self):
        self._check_static()
        snapshot = self._snapshot()
        if self.expected is not None and snapshot != self.expected:
            raise fs.RenameRejected('STOP: filesystem differs from approved cleanup delta')
        self._check_static()
        if self.fd is not None:
            journal._check_parent(self.owner.cleanup_evidence_path, self.parent_fd)
            journal._check_file(self.fd, self.parent_fd, self.owner.cleanup_evidence_path.name)
            self.filesystem._check_fd(self.parent_fd)
            self.filesystem._check_fd(self.fd)
            if os.pread(self.fd, len(self.raw) + 1, 0) != self.raw:
                raise fs.RenameRejected('STOP: cleanup evidence changed')
        return snapshot

    def _append(self, state, **values):
        self._check()
        body = dict(sequence=self.sequence, previous_sha256=self.previous, state=state, **values)
        digest = _digest(body)
        raw = (_canonical(dict(body, sha256=digest)) + '\n').encode('utf-8')
        journal._write_all(self.fd, raw)
        os.fsync(self.fd)
        os.fsync(self.parent_fd)
        self.raw += raw
        self._check()
        self.sequence += 1
        self.previous = digest

    def _proposal(self):
        self.expected = self._check()
        review = self.owner._database._initial_coverage.directories
        current = {e.path: e for e in self.expected}
        candidates = tuple(CleanupCandidate(d.path, (current[d.path].device, current[d.path].inode), d.disposition)
            for d in review if d.disposition in {'CLEANUP_REVIEW', 'UNRESOLVED_EMPTY'}
            and d.contents_inventoried and not d.required_in_target and d.path in current
            and not technical_root(d.path) and not any(p.casefold() == '#recycle' for p in d.path.split('/')))
        payload = self.manifest.payload
        proposal = CleanupProposal(self.manifest.manifest_id,
            hashlib.sha256(self.owner._manifest_bytes.encode()).hexdigest(), str(self.owner.journal_path),
            fs._identity(os.fstat(self.owner._writer._fd)), hashlib.sha256(self.journal_bytes).hexdigest(),
            str(self.owner.root), self.filesystem._root_identity, self.owner.mount_identity,
            self.filesystem._mount_id, payload.filesystem_hash, payload.expected_post_hash,
            self.database_content.digest, self.expected, review, candidates, '')
        return replace(proposal, proposal_sha256=_seal(proposal, 'proposal_sha256'))

    def run(self, decision):
        proposal = self._proposal()
        approval = decision(proposal)
        self._check()
        if approval is None:
            return self._outcome(proposal)
        if (type(approval) is not CleanupApproval
                or approval != approve_cleanup(proposal, approval.directories, actor=approval.actor, reason=approval.reason)):
            raise fs.RenameRejected('STOP: foreign, stale or malformed cleanup approval')
        path = self.owner.cleanup_evidence_path
        self.parent_fd = journal._open_parent(path, self.manifest, (), (self.owner.database_path,))
        self.owner._resources.enter_context(fs._held_evidence_fd(self.parent_fd))
        self.filesystem._check_fd(self.parent_fd)
        self.fd = os.open(path.name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
            | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.parent_fd)
        self.owner._resources.enter_context(fs._held_evidence_fd(self.fd))
        journal._lock(self.fd, True)
        self._append('BOUND', schema='animedb-v6-directory-cleanup', version=1,
            proposal=asdict(proposal), approval=asdict(approval))
        for path in sorted(approval.directories, key=lambda p: (-p.count('/'), p)):
            self._check()
            parts = fs._locator(path)
            parent_path = '/'.join(parts[:-1]) or '.'
            parent, fd = self.pins[parent_path], self.pins[path]
            if os.listdir(fd):
                raise fs.RenameRejected('STOP: approved directory is not empty')
            self._append('INTENT', path=path, identity=fs._identity(os.fstat(fd)),
                parent_identity=fs._identity(os.fstat(parent)))
            self._check()
            if os.listdir(fd):
                raise fs.RenameRejected('STOP: approved directory is no longer empty')
            os.rmdir(parts[-1], dir_fd=parent)
            # Capture only the removed entry and its direct parent's immediate
            # stat delta, before callbacks/barriers can introduce alien changes.
            parent_info = os.fstat(parent)
            self.removed.append(path)
            self.expected = tuple(_entry(parent_path, 'directory', parent_info) if e.path == parent_path else e
                for e in self.expected if e.path != path)
            os.fsync(parent)
            if os.fstat(fd).st_nlink != 0:
                raise fs.RenameRejected('STOP: held approved directory removal not proven')
            # Independently reopen the parent via nofollow traversal, then check
            # exact absence plus all other directories, files/content and DB.
            self.filesystem._parent_binding(parts, parent)
            fs._absent(parent, parts[-1])
            self._check()
            self._append('VERIFIED', path=path, snapshot_sha256=_digest([asdict(e) for e in self.expected]))
        after = self._check()
        result = self._outcome(proposal, after)
        self._append('COMPLETE', removed=result.removed, preserved=result.preserved,
            after_snapshot=[asdict(e) for e in after], after_snapshot_sha256=result.after_snapshot_sha256)
        self._check()
        return result

    def _outcome(self, proposal, after=None):
        after = self.expected if after is None else after
        return CleanupOutcome(self.manifest.manifest_id, proposal.proposal_sha256, tuple(self.removed),
            tuple(e.path for e in after if e.kind == 'directory'), proposal.before_snapshot, after,
            _digest([asdict(e) for e in after]))


def _run_cleanup(owner, decision):
    owner._phase = 'CLEANUP_ADMISSION'
    # Last use of the original E7A/E7D lifecycle: its hash includes old dirs.
    owner._check_execution()
    owner._require_journal()
    owner._database._verify_stored(len(owner._manifest.payload.actions) - 1)
    for index in range(len(owner._manifest.payload.actions)):
        owner._verify_physical(index)
    owner._phase = 'CLEANUP'
    try:
        return _Cleanup(owner).run(decision)
    except Exception as exc:
        # Even a full last frame cannot prove a failed fsync acknowledgement.
        # Keep all bytes and the actual FS; the spent execution always closes.
        if isinstance(exc, ScratchFilesystemReconciliationRequired):
            raise
        raise ScratchFilesystemReconciliationRequired('STOP: cleanup unconfirmed: ' + str(exc), 'CLEANUP') from exc
