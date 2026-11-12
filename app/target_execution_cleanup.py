"""E7H: separately approved rmdir in the still-live, completed scratch owner.

No root adoption, file deletion, retry, resume or E1/manifest mutation. The
original post hash gates admission only; cleanup has its own snapshot contract.
"""
from __future__ import annotations

from contextlib import closing, contextmanager, ExitStack
from dataclasses import asdict, dataclass, fields, replace
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading

from . import target_execution_filesystem as fs
from . import target_execution_journal as journal
from .target_execution_coordination import ScratchFilesystemReconciliationRequired
from .target_execution_database import _rows, _schema
from .target_execution_identity import _hash_file, _file_stat
from .target_execution_manifest import JournalEntry, _canonical, _digest, _decode_type, _strict_json_load, manifest_to_json
from .target_execution_rollback import _owner as _require_maintenance
from .target_planner import relative_locator_safe
from .target_planner_coverage import DirectoryCoverage
from .physical_naming_components import collision_keys
from .target_planner_types import FilesystemEntry, FilesystemSnapshot


def _protected_directory(path):
    """Reorganization cannot remove reserved roots or system directories."""
    if not isinstance(path, str) or path == '.':
        return True
    parts = fs._locator(path)
    normalized = tuple(collision_keys(part).fold_key for part in parts)
    return (normalized[0] in {'import', 'duplicates', '#recycle', '@eadir'}
        or any(part in {'#recycle', '@eadir'} for part in normalized))


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


def _stable_entry(entry):
    # Excluded contents were never inventoried. Only this directory's identity,
    # type and access metadata belong to the reorganization evidence.
    return (replace(entry, nlink=1, size=0, mtime_ns=0, ctime_ns=0)
        if entry.kind == 'system_excluded' else entry)


def _entry(path, kind, info, content=None):
    return _stable_entry(CleanupEntry(path, kind, info.st_dev, info.st_ino, info.st_mode,
        info.st_uid, info.st_gid, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns, content))


def _cleanup_stat(fd, name=None, *, synchronized=False):
    if synchronized:
        return fs._sync_file_metadata(fd, name=name)
    return os.fstat(fd) if name is None else os.stat(name, dir_fd=fd, follow_symlinks=False)


def _confirm_removed_absence(parent, name, metadata):
    # The caller must first authenticate this run's exact VERIFIED removal.
    try:
        metadata(parent, name)
    except FileNotFoundError as exc:
        if exc.errno != errno.ENOENT:
            raise
    else:
        raise fs.RenameRejected('STOP: removed cleanup entry exists again')
    fs._absent(parent, name)


def _snapshot_difference_error(message, expected, actual, *, phase):
    """Bound private numeric diagnostics; no locator or string-field values."""
    before, after = ({entry.path: entry for entry in entries} for entries in (expected, actual))
    changed = [path for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path)]
    records = []
    for path in changed[:8]:
        old, new = before.get(path), after.get(path)
        differences = {}
        if old is None or new is None:
            differences['presence'] = {'expected': int(old is not None), 'actual': int(new is not None)}
        else:
            for field in fields(CleanupEntry):
                a, b = getattr(old, field.name), getattr(new, field.name)
                if field.name != 'path' and a != b:
                    differences[field.name] = {'expected': a, 'actual': b} if type(a) is type(b) is int else {}
        records.append({'record_sha256': hashlib.sha256(path.encode('utf-8')).hexdigest(),
            'differences': differences})
    detail = {'phase': phase, 'different_records': len(changed),
        'omitted_records': max(0, len(changed) - len(records)), 'records': records}
    error = fs.RenameRejected(message + '; CLEANUP_MISMATCH_JSON=' +
        json.dumps(detail, sort_keys=True, separators=(',', ':')))
    error.cleanup_mismatch = detail
    return error


@dataclass(frozen=True)
class CleanupCandidate:
    path: str
    identity: tuple[int, int]
    disposition: str


def _cleanup_candidates(snapshot, directory_review, required_directories):
    """Project removable original subtrees from the verified post-E7 inventory.

    A parent is eligible only when every direct child is itself eligible. Files,
    excluded contents, target parents and incomplete evidence stop propagation.
    Coverage disposition remains pre-E7 evidence, not an emptiness decision.
    """
    review = {row.path: row for row in directory_review}
    required = set(required_directories)
    children = {}
    for entry in snapshot:
        if entry.path != '.':
            parent = entry.path.rsplit('/', 1)[0] if '/' in entry.path else '.'
            children.setdefault(parent, set()).add(entry.path)
    eligible, candidates = set(), []
    for entry in sorted(snapshot, key=lambda row: (-row.path.count('/'), row.path)):
        row = review.get(entry.path)
        if (entry.kind != 'directory' or _protected_directory(entry.path)
                or row is None or not row.contents_inventoried or row.required_in_target
                or entry.path in required or not children.get(entry.path, set()) <= eligible):
            continue
        eligible.add(entry.path)
        candidates.append(CleanupCandidate(entry.path, (entry.device, entry.inode), row.disposition))
    return tuple(candidates)


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
            or any(_protected_directory(p) for p in directories)
            or len(set(directories)) != len(directories)
            or not set(directories) <= {d.path for d in proposal.candidates}
            or type(actor) is not str or not actor.strip() or type(reason) is not str or not reason.strip()):
        raise ValueError('explicit exact candidate paths, actor and reason required')
    # Approving a parent never implicitly approves its empty child directories.
    selected = set(directories)
    for entry in proposal.before_snapshot:
        parts = entry.path.split('/')
        if any('/'.join(parts[:depth]) in selected for depth in range(1, len(parts))) and (
                entry.kind != 'directory' or entry.path not in selected):
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
    mode: str = 'FULL'


_MAX_CLEANUP_BYTES = 64 * 1024 * 1024


@contextmanager
def _local_evidence_parent(path):
    """One admission's local mount snapshot and nofollow directory binding."""
    from .tools.execution_manifest import validate_local_output
    path = Path(path)
    validate_local_output(path)
    with fs._held_evidence_fd(fs._open_absolute(path.parent)) as parent:
        mounts = fs._read_mounts()
        fs._check_mounts(path.parent, parent, mounts, allow_root_mount=True)
        yield parent
        journal._check_parent(path, parent)
        if fs._read_mounts() != mounts:
            raise ValueError('STOP: local evidence mount changed during admission')


def _read_local(path, *, parent=None):
    """Pin local evidence without touching its historical media root."""
    path = Path(path)
    if parent is None:
        with _local_evidence_parent(path) as parent:
            return _read_local(path, parent=parent)
    else:
        with fs._held_evidence_fd(os.open(path.name, fs._FILE, dir_fd=parent)) as fd:
            before = journal._check_file(fd, parent, path.name)
            if before.st_size > _MAX_CLEANUP_BYTES:
                raise ValueError('STOP: cleanup evidence exceeds supported size')
            raw = os.pread(fd, before.st_size + 1, 0)
            after = journal._check_file(fd, parent, path.name)
            if (_file_stat(before) != _file_stat(after) or len(raw) != before.st_size):
                raise ValueError('STOP: cleanup evidence changed while reading')
            return raw, journal._identity(before)


def cleanup_evidence_digest(database_path: Path) -> str:
    """Hash the retained existing E7H artefact; never grant move authority."""
    raw, _ = _read_local(Path(database_path).with_suffix('.cleanup.jsonl'))
    return hashlib.sha256(raw).hexdigest()


def _cleanup_frames(raw, *, terminal='COMPLETE'):
    previous, rows = None, []
    for sequence, frame in enumerate(raw.splitlines(keepends=True)):
        if not frame.endswith(b'\n'):
            raise ValueError('STOP: incomplete cleanup history')
        row = _strict_json_load(frame.decode('utf-8'))
        if type(row) is not dict:
            raise ValueError('STOP: malformed cleanup frame')
        digest = row.pop('sha256')
        if (type(row.get('sequence')) is not int or row['sequence'] != sequence
                or row.get('previous_sha256') != previous or digest != _digest(row)
                or frame != (_canonical(dict(row, sha256=digest)) + '\n').encode()):
            raise ValueError('STOP: cleanup chain/checksum differs')
        previous = digest
        rows.append(row)
    if not rows or rows[0].get('state') != 'BOUND' or rows[-1].get('state') != terminal:
        raise ValueError('STOP: cleanup is not durably complete')
    return rows


def _entries(entries, *, metadata=False):
    result = {entry.path: entry for entry in entries}
    if len(result) != len(entries) or tuple(sorted(result)) != tuple(entry.path for entry in entries):
        raise ValueError('STOP: cleanup snapshot has duplicate or unordered paths')
    if '.' not in result or result['.'].kind != 'directory':
        raise ValueError('STOP: cleanup snapshot lacks its root')
    for entry in entries:
        if entry.path != '.':
            if entry.kind == 'system_excluded':
                if not relative_locator_safe(entry.path) or '\x00' in entry.path:
                    raise ValueError('STOP: unsafe excluded directory path')
            else:
                fs._locator(entry.path)
        if (entry.kind not in {'regular', 'directory', 'system_excluded'}
                or entry.device < 0 or entry.inode <= 0 or entry.nlink < 1
                or entry.size < 0 or entry.mtime_ns < 0 or entry.ctime_ns < 0
                or (entry.kind == 'regular') != stat.S_ISREG(entry.mode)
                or (entry.kind != 'regular') != stat.S_ISDIR(entry.mode)):
            raise ValueError('STOP: cleanup snapshot has unsafe entries')
        if entry.kind == 'regular':
            absent = metadata and entry.content_sha256 is None
            if (entry.nlink != 1 or (not absent and (type(entry.content_sha256) is not str
                    or len(entry.content_sha256) != 64
                    or any(c not in '0123456789abcdef' for c in entry.content_sha256)))):
                raise ValueError('STOP: cleanup file lacks full content evidence')
        elif entry.content_sha256 is not None:
            raise ValueError('STOP: directory cannot carry file content evidence')
        if entry.path != '.':
            parent = entry.path.rsplit('/', 1)[0] if '/' in entry.path else '.'
            if parent not in result or result[parent].kind != 'directory':
                raise ValueError('STOP: cleanup entry has no inventoried parent')
    return result


def _cleanup_delta(proposal, approval, complete, *, metadata=False):
    before = _entries(proposal.before_snapshot, metadata=metadata)
    after_entries = _decode_type(tuple[CleanupEntry, ...], complete['after_snapshot'])
    after = _entries(after_entries, metadata=metadata)
    order = tuple(sorted(approval.directories, key=lambda path: (-path.count('/'), path)))
    if (_decode_type(tuple[str, ...], complete['removed']) != order
            or set(after) != set(before) - set(order)
            or any(path not in before or before[path].kind != 'directory' for path in order)):
        raise ValueError('STOP: cleanup removed an unapproved entry')
    mutable_parents = {path.rsplit('/', 1)[0] if '/' in path else '.' for path in order}
    for path, entry in after.items():
        original = before[path]
        if path in mutable_parents and original.kind == 'directory':
            original = replace(original, size=entry.size, nlink=entry.nlink,
                mtime_ns=entry.mtime_ns, ctime_ns=entry.ctime_ns)
        if _stable_entry(entry) != _stable_entry(original):
            raise ValueError('STOP: cleanup changed an unapproved entry')
    digest = _digest([asdict(entry) for entry in after_entries])
    preserved = tuple(entry.path for entry in after_entries if entry.kind == 'directory')
    if (complete['after_snapshot_sha256'] != digest
            or _decode_type(tuple[str, ...], complete['preserved']) != preserved):
        raise ValueError('STOP: cleanup final snapshot differs')
    return CleanupOutcome(proposal.manifest_id, proposal.proposal_sha256, order, preserved,
        proposal.before_snapshot, after_entries, digest, 'METADATA_RENAME' if metadata else 'FULL')


def _verify_cleanup_filesystem(root, outcome, *, synchronized=False, verified_removed=()):
    expected = _entries(outcome.after_snapshot, metadata=outcome.mode == 'METADATA_RENAME')
    actual = {}
    with fs._held_evidence_fd(fs._open_absolute(root)) as root_fd:
        root_identity, mount_id = fs._identity(os.fstat(root_fd)), fs._mount_id(root_fd)

        def walk(path, fd):
            fs._secure_directory(fd)
            before = _entry(path, 'directory', _cleanup_stat(fd, synchronized=synchronized))
            actual[path] = before
            for name in sorted(os.listdir(fd)):
                child = name if path == '.' else path + '/' + name
                try:
                    named = _cleanup_stat(fd, name, synchronized=synchronized)
                except FileNotFoundError as exc:
                    if exc.errno != errno.ENOENT or not synchronized or child not in verified_removed:
                        raise
                    _confirm_removed_absence(fd, name,
                        lambda fd, name: _cleanup_stat(fd, name, synchronized=True))
                    continue
                entry = expected.get(child)
                if entry is None:
                    raise ValueError('STOP: unexpected file or directory after cleanup')
                if entry.kind == 'system_excluded' and stat.S_ISDIR(named.st_mode):
                    with fs._held_evidence_fd(os.open(name,
                            os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)) as excluded:
                        if fs._identity(os.fstat(excluded)) != fs._identity(named) or fs._mount_id(excluded) != mount_id:
                            raise ValueError('STOP: excluded directory alias or identity differs')
                        actual[child] = _entry(child, 'system_excluded',
                            _cleanup_stat(excluded, synchronized=synchronized))
                    continue
                flags = fs._DIRECTORY if entry.kind == 'directory' else fs._FILE
                with fs._held_evidence_fd(os.open(name, flags, dir_fd=fd)) as held:
                    info = _cleanup_stat(held, synchronized=synchronized)
                    if (fs._identity(info) != fs._identity(named) or info.st_dev != root_identity[0]
                            or fs._mount_id(held) != mount_id):
                        raise ValueError('STOP: cleanup target identity or mount differs')
                    if entry.kind == 'directory':
                        walk(child, held)
                    else:
                        observed = _file_stat(info)
                        digest = None
                        if entry.content_sha256 is not None:
                            observed, content = _hash_file(held)
                            digest = content.digest
                        current = _cleanup_stat(fd, name, synchronized=synchronized)
                        if (observed.st_dev, observed.st_ino, observed.size, observed.mtime_ns, observed.ctime_ns) != (
                                current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns):
                            raise ValueError('STOP: cleanup target changed while hashing')
                        actual[child] = _entry(child, 'regular', current, digest)
            after = _entry(path, 'directory', _cleanup_stat(fd, synchronized=synchronized))
            if before != after:
                raise _snapshot_difference_error('STOP: cleanup directory changed during verification',
                    (before,), (after,), phase='COMPLETED_CLEANUP_VERIFICATION')

        walk('.', root_fd)
        if synchronized:
            # Check even names absent from readdir: a recreated entry must not
            # hide behind the same cache. An absent approved parent also proves
            # its approved descendants absent, without traversing a stale name.
            removed = set(verified_removed)
            for path in sorted(removed):
                parts = fs._locator(path)
                if any('/'.join(parts[:depth]) in removed for depth in range(1, len(parts))):
                    continue
                parent_path = '/'.join(parts[:-1]) or '.'
                parent_entry = expected[parent_path]
                with fs._held_evidence_fd(fs._open_absolute(root / parent_path)) as parent:
                    info = _cleanup_stat(parent, synchronized=True)
                    if (_entry(parent_path, 'directory', info) != parent_entry
                            or fs._mount_id(parent) != mount_id):
                        raise ValueError('STOP: cleanup absence parent binding differs')
                    _confirm_removed_absence(parent, parts[-1],
                        lambda fd, name: _cleanup_stat(fd, name, synchronized=True))
        with fs._held_evidence_fd(fs._open_absolute(root)) as reopened:
            if fs._identity(os.fstat(reopened)) != root_identity or fs._mount_id(reopened) != mount_id:
                raise ValueError('STOP: cleanup root changed during verification')
    if actual != {path: _stable_entry(entry) for path, entry in expected.items()}:
        raise _snapshot_difference_error('STOP: filesystem differs from completed cleanup evidence',
            tuple(_stable_entry(e) for e in expected.values()), tuple(actual.values()),
            phase='COMPLETED_CLEANUP_VERIFICATION')


def _original_directories(database, manifest, proposal, before, parent):
    """Use E7F's retained R0 pins, never a caller-invented cleanup candidate."""
    raw, _ = _read_local(database.with_suffix('.directories.jsonl'), parent=parent)
    frames = _cleanup_frames(raw, terminal='READY')
    bound, ready = frames[0], frames[-1]
    common = {'sequence', 'previous_sha256', 'state'}
    if (bound.get('schema') != 'animedb-v6-directory-preparation'
            or type(bound.get('version')) is not int or bound['version'] != 1
            or bound.get('manifest_id') != proposal.manifest_id
            or bound.get('manifest_sha256') != proposal.manifest_sha256
            or bound.get('root') != proposal.root or bound.get('root_identity') != list(proposal.root_identity)
            or bound.get('mount_identity') != proposal.mount_identity or bound.get('mount_id') != proposal.mount_id
            or bound.get('filesystem_hash') != proposal.original_filesystem_hash
            or bound.get('directories') != list(manifest.payload.directories)
            or type(bound.get('existing')) is not dict):
        raise ValueError('STOP: original directory evidence binding differs')
    original = {path: _decode_type(tuple[int, int], identity) for path, identity in bound['existing'].items()}
    for path, identity in original.items():
        fs._locator(path)
        if path not in before or before[path].kind not in {'directory', 'system_excluded'} or identity != (
                before[path].device, before[path].inode):
            raise ValueError('STOP: original directory identity differs')
    created = tuple(path for path in manifest.payload.directories if path not in original)
    if (set(ready) != common | {'created'} or ready['created'] != list(created)
            or len(frames) != 2 + 2 * len(created)):
        raise ValueError('STOP: directory preparation is incomplete')
    pins = {'.': proposal.root_identity, **original}
    for index, path in enumerate(created):
        intent, verified = frames[1 + 2 * index:3 + 2 * index]
        parent = path.rsplit('/', 1)[0] if '/' in path else '.'
        identity = (before[path].device, before[path].inode) if path in before else None
        if (set(intent) != common | {'path', 'parent_identity'}
                or intent['state'] != 'INTENT' or intent['path'] != path or parent not in pins
                or intent['parent_identity'] != list(pins[parent])
                or set(verified) != common | {'path', 'identity', 'mtime_ns', 'parent_mtime_ns'}
                or verified['state'] != 'VERIFIED' or verified['path'] != path
                or identity is None or verified['identity'] != list(identity)):
            raise ValueError('STOP: directory preparation acknowledgement differs')
        pins[path] = identity
    return original


def read_completed_cleanup(database_path: Path, manifest, root: Path, *, verify_filesystem: bool = True) -> CleanupOutcome:
    """Validate existing E7H history; this never creates execution authority.

    Historical NORMAL admission checks retained bytes only. Pre-NORMAL callers
    additionally prove the current final DB and exact post-cleanup filesystem.
    """
    database, root = Path(database_path), Path(root)
    if not database.is_absolute() or database.is_relative_to(root) or str(root) != manifest.payload.library_root:
        raise ValueError('STOP: cleanup database/library binding differs')
    with _local_evidence_parent(database) as parent:
        return _read_completed_cleanup(database, manifest, root, parent, verify_filesystem=verify_filesystem)


def _read_completed_cleanup(database, manifest, root, parent, *, verify_filesystem):
    try:
        raw, _ = _read_local(database.with_suffix('.cleanup.jsonl'), parent=parent)
        frames = _cleanup_frames(raw)
        bound, complete = frames[0], frames[-1]
        common = {'sequence', 'previous_sha256', 'state'}
        metadata = bound.get('version') == 2
        if (set(bound) != common | {'schema', 'version', 'proposal', 'approval'} | ({'mode'} if metadata else set())
                or bound['schema'] != 'animedb-v6-directory-cleanup' or type(bound['version']) is not int
                or bound['version'] not in (1, 2) or (metadata and bound['mode'] != 'METADATA_RENAME')
                or set(complete) != common | {
                    'removed', 'preserved', 'after_snapshot', 'after_snapshot_sha256'}):
            raise ValueError('STOP: unsupported cleanup history')
        proposal = _decode_type(CleanupProposal, bound['proposal'])
        approval = _decode_type(CleanupApproval, bound['approval'])
        payload = manifest.payload
        if (proposal.manifest_id != manifest.manifest_id
                or proposal.manifest_sha256 != hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest()
                or proposal.root != str(root) or proposal.execution_journal != str(database.with_suffix('.journal.jsonl'))
                or proposal.original_filesystem_hash != payload.filesystem_hash
                or proposal.expected_post_hash != payload.expected_post_hash
                or proposal.mount_identity != payload.mount_identity):
            raise ValueError('STOP: cleanup proposal differs from approved execution')
        before = _entries(proposal.before_snapshot, metadata=metadata)
        if (proposal.root_identity != (before['.'].device, before['.'].inode)
                or {entry.path for entry in proposal.before_snapshot if entry.kind == 'regular'} != {a.target for a in payload.actions}):
            raise ValueError('STOP: cleanup proposal lacks original targets/root identity')
        if any((before[action.target].size, before[action.target].mtime_ns) != (
                action.expected_size, action.expected_mtime_ns) for action in payload.actions):
            raise ValueError('STOP: cleanup file metadata differs from approved execution')
        from .target_execution_identity import _decode_frame
        for index, action in enumerate(payload.actions):
            evidence = _decode_frame(_read_local(database.with_suffix(f'.identity-{index}.json'), parent=parent)[0])
            entry = before[action.target]
            if (evidence.manifest_id != manifest.manifest_id or evidence.action != action
                    or evidence.action_index != index or evidence.manifest_sha256 != proposal.manifest_sha256
                    or evidence.binding.root != str(root)
                    or (evidence.binding.root_identity.st_dev, evidence.binding.root_identity.st_ino) != proposal.root_identity
                    or (evidence.filesystem.st_dev, evidence.filesystem.st_ino,
                        evidence.filesystem.size, evidence.filesystem.mtime_ns) != (
                            entry.device, entry.inode, entry.size, entry.mtime_ns)
                    or (not metadata and evidence.mode != 'FULL')
                    or entry.content_sha256 != (evidence.content.digest if evidence.content is not None else None)):
                raise ValueError('STOP: cleanup identity/content mode differs from execution evidence')
        original_directories = _original_directories(database, manifest, proposal, before, parent)
        review = {entry.path: entry for entry in proposal.directory_review}
        if len(review) != len(proposal.directory_review) or len({c.path for c in proposal.candidates}) != len(proposal.candidates):
            raise ValueError('STOP: ambiguous cleanup candidates')
        eligible = set(_cleanup_candidates(proposal.before_snapshot, proposal.directory_review, payload.directories))
        for candidate in proposal.candidates:
            entry, coverage = before.get(candidate.path), review.get(candidate.path)
            if (_protected_directory(candidate.path) or entry is None or entry.kind != 'directory'
                    or candidate.identity != (entry.device, entry.inode) or coverage is None
                    or original_directories.get(candidate.path) != candidate.identity
                    or coverage.required_in_target or not coverage.contents_inventoried
                    or coverage.disposition != candidate.disposition
                    or candidate not in eligible
                    or candidate.path in payload.directories):
                raise ValueError('STOP: cleanup candidate lacks original inventory authority')
        if approval != approve_cleanup(proposal, approval.directories, actor=approval.actor, reason=approval.reason):
            raise ValueError('STOP: cleanup approval differs')
        outcome = _cleanup_delta(proposal, approval, complete, metadata=metadata)
        if len(frames) != 2 + 2 * len(outcome.removed):
            raise ValueError('STOP: cleanup action history is incomplete')
        replay = dict(before)
        for index, path in enumerate(outcome.removed):
            intent, verified = frames[1 + 2 * index:3 + 2 * index]
            parent_path = path.rsplit('/', 1)[0] if '/' in path else '.'
            if (set(intent) != common | {'path', 'identity', 'parent_identity'}
                    or intent['state'] != 'INTENT' or intent['path'] != path
                    or intent['identity'] != [before[path].device, before[path].inode]
                    or intent['parent_identity'] != [before[parent_path].device, before[parent_path].inode]
                    or verified['state'] != 'VERIFIED' or verified['path'] != path
                    or set(verified) != common | ({'path', 'parent_after', 'delta_sha256'} if metadata else {'path', 'snapshot_sha256'})):
                raise ValueError('STOP: cleanup acknowledgement differs')
            if metadata:
                parent_after = _decode_type(CleanupEntry, verified['parent_after'])
                original_parent = replay[parent_path]
                immutable_parent = replace(original_parent, size=parent_after.size, nlink=parent_after.nlink,
                    mtime_ns=parent_after.mtime_ns, ctime_ns=parent_after.ctime_ns)
                if (parent_after.nlink < 1 or parent_after.size < 0
                        or parent_after.mtime_ns < 0 or parent_after.ctime_ns < 0
                        or parent_after != immutable_parent
                        or verified['delta_sha256'] != _digest({'path': path, 'parent_after': verified['parent_after']})):
                    raise ValueError('STOP: cleanup parent delta differs')
                replay.pop(path)
                replay[parent_path] = parent_after
            elif (type(verified['snapshot_sha256']) is not str or len(verified['snapshot_sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in verified['snapshot_sha256'])):
                raise ValueError('STOP: cleanup snapshot checksum differs')
        if metadata and tuple(replay[path] for path in sorted(replay)) != outcome.after_snapshot:
            raise ValueError('STOP: cleanup delta history differs from final snapshot')
        if not metadata and frames[-2]['snapshot_sha256'] != outcome.after_snapshot_sha256:
            raise ValueError('STOP: cleanup final acknowledgement differs')
        journal_path = database.with_suffix('.journal.jsonl')
        raw_journal, journal_identity = _read_local(journal_path, parent=parent)
        if (journal_identity != proposal.execution_identity
                or hashlib.sha256(raw_journal).hexdigest() != proposal.execution_sha256):
            raise ValueError('STOP: original complete execution journal changed')
        # No shared flock: the live executor may still hold its exclusive writer.
        with fs._held_evidence_fd(os.open(journal_path.name, fs._FILE, dir_fd=parent)) as fd:
            before_journal = journal._check_file(fd, parent, journal_path.name)
            expected = tuple(JournalEntry(manifest.manifest_id, action.action_id, index, state)
                for index, action in enumerate(payload.actions)
                for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'))
            if (journal._identity(before_journal) != journal_identity
                    or os.pread(fd, len(raw_journal) + 1, 0) != raw_journal
                    or journal._read_history(fd, manifest) != expected
                    or _file_stat(journal._check_file(fd, parent, journal_path.name)) != _file_stat(before_journal)):
                raise ValueError('STOP: original execution is incomplete or changed')
        if verify_filesystem:
            with fs._held_evidence_fd(os.open(database.name, fs._FILE, dir_fd=parent)) as fd:
                journal._check_file(fd, parent, database.name)
                if _hash_file(fd)[1].digest != proposal.database_sha256:
                    raise ValueError('STOP: final database changed after cleanup')
                journal._check_file(fd, parent, database.name)
            from .target_execution_guard import _maintenance_record
            binding = _maintenance_record(database)
            synchronized = binding is not None and binding['version'] == 2
            if manifest.payload.mount_identity.startswith('cifs:') and not synchronized:
                raise ValueError('STOP: production cleanup maintenance binding unavailable')
            if synchronized and binding['library'] != str(root):
                raise ValueError('STOP: cleanup maintenance library differs')
            # All INTENT/VERIFIED pairs above have been authenticated against
            # this original journal, proposal, approval and E1 execution.
            _verify_cleanup_filesystem(root, outcome, synchronized=synchronized,
                verified_removed=outcome.removed)
        if _read_local(database.with_suffix('.cleanup.jsonl'), parent=parent)[0] != raw:
            raise ValueError('STOP: cleanup history changed during verification')
        return outcome
    except (KeyError, TypeError, UnicodeError, IndexError) as exc:
        raise ValueError('STOP: malformed completed cleanup history') from exc


def restore_cleanup_directories(snapshot: FilesystemSnapshot, outcome: CleanupOutcome, *,
                                manifest=None, database_path=None) -> FilesystemSnapshot:
    """Reconstruct only deleted directory entries for the original post hash.

    The caller must first validate the retained cleanup chain and current state.
    No filesystem or manifest is changed and no new baseline is sealed.
    """
    entries = {entry.relative_path: entry for entry in snapshot.entries}
    if manifest is not None and manifest.payload.mount_identity.startswith('cifs:'):
        # The planner's ordinary scandir can retain these names too. Authenticate
        # the same completed journal and FORCE_SYNC the whole current filesystem
        # (including exact removed-path absence) before discarding its stale rows.
        checked = read_completed_cleanup(database_path, manifest, Path(snapshot.root))
        if checked != outcome:
            raise ValueError('STOP: cleanup reconstruction evidence differs')
        for path in outcome.removed:
            entries.pop(path, None)
    before = {entry.path: entry for entry in outcome.before_snapshot}
    for path in outcome.removed:
        original = before.get(path)
        if path in entries or original is None or original.kind != 'directory' or _protected_directory(path):
            raise ValueError('STOP: invalid cleanup reconstruction')
        entries[path] = FilesystemEntry(path, 'directory', mtime_ns=original.mtime_ns)
    return replace(snapshot, entries=tuple(entries[path] for path in sorted(entries)))


class _Cleanup:
    def __init__(self, owner):
        self.owner, self.filesystem = owner, owner._filesystem
        self.manifest = owner._manifest
        self.pins = {'.': self.filesystem._root_identity, **owner._directories.pins}
        self.removed = []
        self.raw, self.previous, self.sequence = bytearray(), None, 0
        self._artifact_checkpoint = None
        self._last_raw, self._last_offset = b'', 0
        self.fd = self.parent_fd = None
        self._expected_entries = None
        self._active_paths = ()
        self.journal_bytes = os.pread(owner._writer._fd, os.fstat(owner._writer._fd).st_size + 1, 0)
        self.database_stat, self.database_content = _hash_file(owner._database._fd)
        self.preparation_stat = _file_stat(os.fstat(owner._directories.fd))

    @property
    def expected(self):
        return tuple(self._expected_entries[path] for path in sorted(self._expected_entries))

    def _stat(self, fd, name=None):
        return _cleanup_stat(fd, name, synchronized=bool(self.owner._production_binding))

    def _check_static(self, *, full=False):
        owner, filesystem, db = self.owner, self.filesystem, self.owner._database
        if (owner._closed or os.getpid() != owner._pid or not owner._mutex.locked()
                or threading.get_ident() != owner._execution_thread
                or self.manifest is not owner._manifest):
            raise fs.RenameRejected('STOP: cleanup lost its live execution owner')
        _require_maintenance(
            owner._maintenance_owner, owner.root, database_path=owner.database_path,
        )
        filesystem._check_root()
        owner._persistent_guard.check()
        owner._check_journal()
        if full:
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
        self.owner._check_storage_fd(db._fd)
        self.owner._check_storage_fd(db._parent)
        if (_file_stat(os.fstat(db._fd)) != self.database_stat
                or not db._connection.in_transaction
                or db._connection.total_changes != db._change_counts[-1]
                or db._connection.execute('PRAGMA data_version').fetchone()[0] != db._version):
            raise fs.RenameRejected('STOP: final DB projection changed during cleanup')
        if full:
            with closing(sqlite3.connect(db._uri + '?mode=ro', uri=True, timeout=0)) as fresh:
                fresh.execute('PRAGMA query_only=ON')
                fresh.execute('BEGIN')
                if _rows(fresh) != db._states[-1] or _schema(fresh) != db._schema:
                    raise fs.RenameRejected('STOP: independent final DB projection differs')
        preparation = owner._directories
        journal._check_parent(owner.directory_evidence_path, preparation.parent_fd)
        journal._check_file(preparation.fd, preparation.parent_fd, owner.directory_evidence_path.name)
        if (_file_stat(os.fstat(preparation.fd)) != self.preparation_stat or
                (full and os.pread(preparation.fd, len(preparation.raw) + 1, 0) != preparation.raw)):
            raise fs.RenameRejected('STOP: original directory evidence changed')

    def _snapshot(self, *, full=False):
        """Whole-state nofollow metadata inventory, without implicit media reads.

        Only a source explicitly selected for FULL verification has a digest.
        Ordinary METADATA_RENAME entries retain content_sha256=None throughout.
        """
        filesystem = self.filesystem
        filesystem._check_root()
        removed = set(self.removed)
        pins = {p: identity for p, identity in self.pins.items() if p not in removed}
        targets = {a.target: (i, a) for i, a in enumerate(self.manifest.payload.actions)}
        excluded = {e.relative_path for e in self.owner._database._initial_snapshot.entries if e.kind == 'system_excluded'}
        entries = {'.': _entry('.', 'directory', self._stat(filesystem._root_fd))}
        removal_evidence = {}
        for path in sorted(pins):
            with self.owner._directories.open(path) as parent:
                self._snapshot_directory(path, parent, pins, targets, excluded, entries,
                    full=full, removal_evidence=removal_evidence)
        if set(entries) != set(pins) | set(targets) | excluded:
            raise fs.RenameRejected('STOP: cleanup inventory has missing entries')
        filesystem._check_root()
        return tuple(entries[p] for p in sorted(entries))

    def _snapshot_names(self, parent):
        if not self.owner._production_binding or not self.removed:
            return sorted(os.listdir(parent))
        # Avoid sharing the old open-file description, but CIFS may still return
        # stale names on this independent handle. Each name needs metadata proof.
        with fs._held_evidence_fd(os.open('.', fs._DIRECTORY, dir_fd=parent)) as current:
            self.filesystem._check_fd(current)
            if fs._identity(os.fstat(current)) != fs._identity(os.fstat(parent)):
                raise fs.RenameRejected('STOP: cleanup enumeration directory identity changed')
            return sorted(os.listdir(current))

    def _verified_removals(self):
        """Read the original live journal, not merely the in-memory removed list."""
        self._check()
        raw = bytes(self.raw)
        if os.pread(self.fd, len(raw) + 1, 0) != raw:
            raise fs.RenameRejected('STOP: cleanup evidence changed')
        terminal = _strict_json_load(self._last_raw.decode('utf-8'))['state']
        if terminal not in {'VERIFIED', 'COMPLETE'}:
            raise fs.RenameRejected('STOP: cleanup removal lacks VERIFIED evidence')
        frames = _cleanup_frames(raw, terminal=terminal)
        bound = frames[0]
        proposal = _decode_type(CleanupProposal, bound['proposal'])
        approval = _decode_type(CleanupApproval, bound['approval'])
        if (bound.get('schema') != 'animedb-v6-directory-cleanup'
                or bound.get('version') != 2 or bound.get('mode') != 'METADATA_RENAME'
                or proposal.manifest_id != self.manifest.manifest_id
                or proposal.manifest_sha256 != hashlib.sha256(self.owner._manifest_bytes.encode()).hexdigest()
                or approval != approve_cleanup(proposal, approval.directories,
                    actor=approval.actor, reason=approval.reason)):
            raise fs.RenameRejected('STOP: cleanup removal approval binding differs')
        order = sorted(approval.directories, key=lambda p: (-p.count('/'), p))
        actions = frames[1:-1] if terminal == 'COMPLETE' else frames[1:]
        if order != self.removed or len(actions) != 2 * len(order):
            raise fs.RenameRejected('STOP: cleanup removal history is incomplete')
        for index, path in enumerate(order):
            intent, verified = actions[2 * index:2 * index + 2]
            parent = path.rsplit('/', 1)[0] if '/' in path else '.'
            if (intent.get('state') != 'INTENT' or intent.get('path') != path
                    or intent.get('identity') != list(self.pins[path])
                    or intent.get('parent_identity') != list(self.pins[parent])
                    or verified.get('state') != 'VERIFIED' or verified.get('path') != path
                    or verified.get('delta_sha256') != _digest({
                        'path': path, 'parent_after': verified.get('parent_after')})):
                raise fs.RenameRejected('STOP: cleanup removal lacks matching VERIFIED evidence')
        self._check()
        return frozenset(order)

    def _removed_entry_absent(self, child, parent, name, evidence):
        """Only explain stale CIFS readdir for this run's durable own rmdir."""
        if (not self.owner._production_binding or child not in self.removed
                or getattr(self, 'fd', None) is None):
            return False
        # Parse the potentially large proposal once per snapshot, only if needed.
        if 'verified' not in evidence:
            evidence['verified'] = self._verified_removals()
        if child not in evidence['verified']:
            return False
        _confirm_removed_absence(parent, name, self._stat)
        return True

    def _snapshot_directory(self, path, parent, pins, targets, excluded, entries, *, full, removal_evidence):
        filesystem = self.filesystem
        filesystem._check_root()
        if path != '.':
            filesystem._parent_binding(fs._locator(path) + ('<child>',), parent)
        before = _entry(path, 'directory', self._stat(parent))
        if self.owner._production_binding:
            if path in entries and entries[path] != before:
                raise _snapshot_difference_error('STOP: cleanup directory changed while opening',
                    (entries[path],), (before,), phase=self.owner._phase)
            entries[path] = before
        for name in self._snapshot_names(parent):
            child = name if path == '.' else path + '/' + name
            try:
                info = self._stat(parent, name)
            except FileNotFoundError as exc:
                if exc.errno != errno.ENOENT or not self._removed_entry_absent(
                        child, parent, name, removal_evidence):
                    raise
                continue
            if child in excluded and stat.S_ISDIR(info.st_mode):
                entries[child] = _entry(child, 'system_excluded', info)
            elif child in pins and child != '.' and stat.S_ISDIR(info.st_mode):
                if fs._identity(info) != pins[child]:
                    raise fs.RenameRejected('STOP: cleanup directory identity changed')
                entries[child] = _entry(child, 'directory', info)
            elif child in targets and stat.S_ISREG(info.st_mode):
                index, action = targets[child]
                self.owner._verify_physical(index, full=full)
                current = self._stat(parent, name)
                if _file_stat(current) != self.owner._completed_stats[index]:
                    raise fs.RenameRejected('STOP: file changed during cleanup inventory')
                content = self.owner._prepared[index][3]
                entries[child] = _entry(child, 'regular', current, content.digest if content is not None else None)
            else:
                raise fs.RenameRejected('STOP: unknown content, symlink or unexpected directory: ' + child)
        after = _entry(path, 'directory', self._stat(parent))
        if before != after:
            raise _snapshot_difference_error('STOP: directory changed during cleanup inventory',
                (before,), (after,), phase=self.owner._phase)

    def _check(self, *, full=False):
        self._check_static(full=full)
        snapshot = self._snapshot(full=full) if full else None
        if full and _hash_file(self.owner._database._fd) != (self.database_stat, self.database_content):
            raise fs.RenameRejected('STOP: final DB content changed during cleanup')
        if full and self._expected_entries is not None and snapshot != self.expected:
            raise _snapshot_difference_error('STOP: filesystem differs from approved cleanup delta',
                self.expected, snapshot, phase=self.owner._phase)
        if not full:
            for path in self._active_paths:
                if path in self.owner._directories.removed:
                    continue
                with self.owner._directories.open(path) as fd:
                    current = _entry(path, 'directory', self._stat(fd))
                    if current != self._expected_entries[path]:
                        raise _snapshot_difference_error('STOP: active cleanup directory changed',
                            (self._expected_entries[path],), (current,), phase=self.owner._phase)
        self._check_static(full=full)
        if self.fd is not None:
            journal._check_parent(self.owner.cleanup_evidence_path, self.parent_fd)
            journal._check_file(self.fd, self.parent_fd, self.owner.cleanup_evidence_path.name)
            self.owner._check_storage_fd(self.parent_fd)
            self.owner._check_storage_fd(self.fd)
            if (journal._checkpoint_stat(os.fstat(self.fd)) != self._artifact_checkpoint
                    or os.pread(self.fd, len(self._last_raw) + 1, self._last_offset) != self._last_raw
                    or (full and os.pread(self.fd, len(self.raw) + 1, 0) != self.raw)):
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
        self._last_offset, self._last_raw = len(self.raw), raw
        self._artifact_checkpoint = journal._checkpoint_stat(os.fstat(self.fd))
        self.raw += raw
        self._check()
        self.sequence += 1
        self.previous = digest

    def _proposal(self):
        self._expected_entries = {entry.path: entry for entry in self._check(full=True)}
        review = self.owner._database._initial_coverage.directories
        payload = self.manifest.payload
        candidates = _cleanup_candidates(self.expected, review, payload.directories)
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
        self._check(full=True)
        if approval is None:
            return self._outcome(proposal)
        if (type(approval) is not CleanupApproval
                or approval != approve_cleanup(proposal, approval.directories, actor=approval.actor, reason=approval.reason)):
            raise fs.RenameRejected('STOP: foreign, stale or malformed cleanup approval')
        path = self.owner.cleanup_evidence_path
        self.parent_fd = journal._open_parent(path, self.manifest, (), (self.owner.database_path,))
        self.owner._resources.enter_context(fs._held_evidence_fd(self.parent_fd))
        self.owner._check_storage_fd(self.parent_fd)
        self.fd = os.open(path.name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
            | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.parent_fd)
        self.owner._resources.enter_context(fs._held_evidence_fd(self.fd))
        journal._lock(self.fd, True)
        self._artifact_checkpoint = journal._checkpoint_stat(os.fstat(self.fd))
        self._append('BOUND', schema='animedb-v6-directory-cleanup', version=2, mode='METADATA_RENAME',
            proposal=asdict(proposal), approval=asdict(approval))
        for path in sorted(approval.directories, key=lambda p: (-p.count('/'), p)):
            self._check()
            if _protected_directory(path):
                raise fs.RenameRejected('STOP: protected directory cannot be removed')
            parts = fs._locator(path)
            parent_path = '/'.join(parts[:-1]) or '.'
            self._active_paths = (parent_path, path)
            with ExitStack() as active:
                parent = active.enter_context(self.owner._directories.open(parent_path))
                candidate = active.enter_context(ExitStack())
                fd = candidate.enter_context(self.owner._directories.open(path))
                self._remove(path, parts, parent_path, parent, fd, release_candidate=candidate.close)
        self._active_paths = ()
        after = self._check(full=True)
        result = self._outcome(proposal, after)
        self._append('COMPLETE', removed=result.removed, preserved=result.preserved,
            after_snapshot=[asdict(e) for e in after], after_snapshot_sha256=result.after_snapshot_sha256)
        self._check(full=True)
        return result

    def _remove(self, path, parts, parent_path, parent, fd, *, release_candidate):
        if os.listdir(fd):
            raise fs.RenameRejected('STOP: approved directory is not empty')
        self._append('INTENT', path=path, identity=fs._identity(os.fstat(fd)),
            parent_identity=fs._identity(os.fstat(parent)))
        self._check()
        if os.listdir(fd):
            raise fs.RenameRejected('STOP: approved directory is no longer empty')
        if _protected_directory(path):
            raise fs.RenameRejected('STOP: protected directory cannot be removed')
        os.rmdir(parts[-1], dir_fd=parent)
        if self.owner._production_binding:
            # SMB may defer the server's parent timestamp update until the
            # removed directory handle closes. Prove the held inode was unlinked
            # first, then close that pin before capturing the server's delta.
            if os.fstat(fd).st_nlink != 0:
                raise fs.RenameRejected('STOP: held approved directory removal not proven')
            self.owner._directories.removed.add(path)
            release_candidate()
        # A CIFS mount-root can still expose pre-rmdir metadata at this point.
        # Establish named absence before capturing the parent delta; retain the
        # later absence fence against reappearance before VERIFIED.
        self.filesystem._parent_binding(parts, parent)
        fs._absent(parent, parts[-1])
        # Capture only this removal's direct-parent delta, before any callbacks.
        parent_info = self._stat(parent)
        self.removed.append(path)
        self.owner._directories.removed.add(path)
        self._expected_entries.pop(path)
        self._expected_entries[parent_path] = _entry(parent_path, 'directory', parent_info)
        os.fsync(parent)
        if not self.owner._production_binding and os.fstat(fd).st_nlink != 0:
            raise fs.RenameRejected('STOP: held approved directory removal not proven')
        # Independently reopen the parent via nofollow traversal, then check
        # exact absence plus all other directories, files/content and DB.
        self.filesystem._parent_binding(parts, parent)
        fs._absent(parent, parts[-1])
        self._check()
        parent_after = asdict(self._expected_entries[parent_path])
        self._append('VERIFIED', path=path, parent_after=parent_after,
            delta_sha256=_digest({'path': path, 'parent_after': parent_after}))

    def _outcome(self, proposal, after=None):
        after = self.expected if after is None else after
        return CleanupOutcome(self.manifest.manifest_id, proposal.proposal_sha256, tuple(self.removed),
            tuple(e.path for e in after if e.kind == 'directory'), proposal.before_snapshot, after,
            _digest([asdict(e) for e in after]), 'METADATA_RENAME')


def _run_cleanup(owner, decision):
    owner._phase = 'CLEANUP_ADMISSION'
    # Last use of the original E7A/E7D lifecycle: its hash includes old dirs.
    owner._check_execution()
    owner._require_journal()
    owner._database._verify_stored(len(owner._manifest.payload.actions) - 1)
    for index in range(len(owner._manifest.payload.actions)):
        owner._verify_physical(index, full=True)
    owner._phase = 'CLEANUP'
    try:
        # Completion retains the original acknowledged bytes and held artefacts,
        # not merely a later reload with the same removed paths and snapshots.
        owner._cleanup_verifier = _Cleanup(owner)
        return owner._cleanup_verifier.run(decision)
    except Exception as exc:
        # Even a full last frame cannot prove a failed fsync acknowledgement.
        # Keep all bytes and the actual FS; the spent execution always closes.
        if isinstance(exc, ScratchFilesystemReconciliationRequired):
            raise
        raise ScratchFilesystemReconciliationRequired('STOP: cleanup unconfirmed: ' + str(exc), 'CLEANUP') from exc
