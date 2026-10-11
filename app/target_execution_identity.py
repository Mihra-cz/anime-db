"""E3A standalone scratch identity evidence, never execution/recovery authority.

No existing-root adoption, rename, journal/DB writes or restart/resume API.
Content equality does not prove physical inode origin after live evidence loss.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import threading

from . import target_execution_filesystem as fs
from . import target_execution_journal as journal
from .target_execution_manifest import (
    ExecutionManifest, ManifestAction, _canonical, _decode_type, _digest,
    _strict_json_load, manifest_to_json,
)

_SCHEMA = 'animedb-v6-file-identity'
_VERSION = 1
_MAX_RECORD_BYTES = 65536
_MAX_FILE_BYTES = 8 * 1024 * 1024  # Fixed scratch boundary, not a video fingerprint policy.
_CHUNK_BYTES = 65536


class IdentityEvidenceRejected(ValueError):
    """STOP: invalid binding/path/record; no execution authority."""


class IdentityEvidenceReconciliationRequired(IdentityEvidenceRejected):
    """STOP: capture/storage acknowledgement or source stability is uncertain."""


@dataclass(frozen=True)
class DirectoryIdentity:
    st_dev: int
    st_ino: int


@dataclass(frozen=True)
class FileStatEvidence:
    st_dev: int
    st_ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    file_type: str
    nlink: int
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class ContentEvidence:
    algorithm: str
    digest: str
    coverage: str
    bytes_hashed: int


@dataclass(frozen=True)
class ScratchBinding:
    root: str
    root_identity: DirectoryIdentity
    source_parent: DirectoryIdentity
    target_parent: DirectoryIdentity
    mount_id: int
    mount_fstype: str
    mount_source: str
    mount_point: str
    mount_fs_root: str
    mount_record_sha256: str
    mount_table_sha256: str


@dataclass(frozen=True)
class FileIdentityEvidence:
    schema: str
    version: int
    manifest_id: str
    manifest_sha256: str
    action_index: int
    action: ManifestAction
    binding: ScratchBinding
    filesystem: FileStatEvidence
    content: ContentEvidence


@dataclass(frozen=True)
class LocatorComparison:
    locator: str
    presence: str
    observed_file: FileStatEvidence | None
    observed_content: ContentEvidence | None
    filesystem_match: str
    content_match: str
    physical_identity_match: str
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class IdentityComparison:
    outcome: str
    source: LocatorComparison | None
    target: LocatorComparison | None
    diagnostics: tuple[str, ...]


def _directory(fd: int) -> DirectoryIdentity:
    info = os.fstat(fd)
    return DirectoryIdentity(info.st_dev, info.st_ino)


def _file_stat(info: os.stat_result) -> FileStatEvidence:
    return FileStatEvidence(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
        info.st_ctime_ns, 'regular' if stat.S_ISREG(info.st_mode) else 'other',
        info.st_nlink, info.st_mode, info.st_uid, info.st_gid)


def _safe_file(value: FileStatEvidence) -> None:
    if (value.file_type != 'regular' or not stat.S_ISREG(value.mode) or value.nlink != 1
            or value.uid != os.geteuid() or value.mode & 0o022 or value.st_dev < 0
            or value.st_ino <= 0 or value.gid < 0 or value.ctime_ns < 0
            or value.mtime_ns < 0 or not 0 <= value.size <= _MAX_FILE_BYTES):
        raise IdentityEvidenceRejected('STOP: independent private regular scratch file required')


def _hash_file(fd: int) -> tuple[FileStatEvidence, ContentEvidence]:
    before = _file_stat(os.fstat(fd))
    _safe_file(before)
    digest, offset = hashlib.sha256(), 0
    while True:
        chunk = os.pread(fd, _CHUNK_BYTES, offset)
        if not chunk:
            break
        offset += len(chunk)
        if offset > before.size:
            raise IdentityEvidenceReconciliationRequired('STOP: file grew while hashing')
        digest.update(chunk)
    after = _file_stat(os.fstat(fd))
    if before != after or offset != before.size:
        raise IdentityEvidenceReconciliationRequired('STOP: file changed while hashing')
    return before, ContentEvidence('sha256', digest.hexdigest(), 'FULL', offset)


def _frame(evidence: FileIdentityEvidence) -> bytes:
    body = asdict(evidence)
    raw = (_canonical({'evidence': body, 'sha256': _digest(body)}) + '\n').encode('utf-8')
    if len(raw) > _MAX_RECORD_BYTES:
        raise IdentityEvidenceRejected('STOP: evidence record exceeds scratch limit')
    return raw


def _decode_frame(raw: bytes) -> FileIdentityEvidence:
    try:
        if len(raw) > _MAX_RECORD_BYTES or not raw.endswith(b'\n'):
            raise ValueError('incomplete or oversized record')
        value = _strict_json_load(raw.decode('utf-8'))
        if type(value) is not dict or set(value) != {'evidence', 'sha256'}:
            raise ValueError('unknown or missing record fields')
        evidence = _decode_type(FileIdentityEvidence, value['evidence'])
        if raw != _frame(evidence):
            raise ValueError('noncanonical bytes or checksum mismatch')
        return evidence
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError) as exc:
        raise IdentityEvidenceRejected('STOP: invalid evidence record: ' + str(exc)) from exc


def _write_record(fd: int, raw: bytes) -> None:
    remaining = memoryview(raw)
    while remaining:
        count = os.write(fd, remaining)
        if count <= 0:
            raise OSError('evidence write made no progress')
        remaining = remaining[count:]


class ScratchIdentitySession:
    """New E2A scratch root and one adjacent, exclusive identity artefact.

    Capture once. Load/diagnostics remain read-only after close, on this minted
    context only; no API can adopt a root or mint restart execution authority.
    """

    def __init__(self, *_args, **_kwargs):
        raise TypeError('use ScratchIdentitySession.create; no existing-root adoption')

    @classmethod
    def create(cls, parent: Path) -> ScratchIdentitySession:
        filesystem = fs.ScratchRenameSession.create(parent)
        try:
            session = object.__new__(cls)
            session._filesystem = filesystem
            session._evidence_path = filesystem.root.parent / (filesystem.root.name + '.identity.json')
            session._resources = ExitStack()
            session._resources.enter_context(filesystem)
            session._pid = os.getpid()
            session._mutex = threading.Lock()
            session._attempted = session._closed = session._poisoned = session._durable = False
            session._pin_fd = None
            session._source_parent_fd = session._target_parent_fd = None
            session._artifact_fd = session._artifact_parent_fd = None
            session._captured_record = session._artifact_stat = None
            session._mount = next(m for m in filesystem._mounts if m.mount_id == filesystem._mount_id)
            return session
        except BaseException as exc:
            filesystem.__exit__(type(exc), exc, exc.__traceback__)
            raise

    @property
    def root(self) -> Path:
        return self._filesystem.root

    @property
    def mount_identity(self) -> str:
        return self._filesystem.mount_identity

    @property
    def evidence_path(self) -> Path:
        return self._evidence_path

    def _bind(self, manifest: ExecutionManifest, action: ManifestAction, index: int) -> str:
        if os.getpid() != self._pid:
            raise IdentityEvidenceRejected('STOP: inherited identity session')
        try:
            self._filesystem._validate(manifest, action)
            if (type(index) is not int or not 0 <= index < len(manifest.payload.actions)
                    or action != manifest.payload.actions[index] or action.action not in {'MOVE', 'QUARANTINE'}):
                raise IdentityEvidenceRejected('STOP: exact MOVE/QUARANTINE action/index required')
            return hashlib.sha256(manifest_to_json(manifest).encode('utf-8')).hexdigest()
        except (ValueError, TypeError, AttributeError) as exc:
            if isinstance(exc, IdentityEvidenceRejected):
                raise
            raise IdentityEvidenceRejected('STOP: immutable scratch manifest/action binding rejected') from exc

    def _binding(self, source_parent: int, target_parent: int) -> ScratchBinding:
        mount, filesystem = self._mount, self._filesystem
        return ScratchBinding(str(self.root), DirectoryIdentity(*filesystem._root_identity),
            _directory(source_parent), _directory(target_parent), mount.mount_id,
            mount.fstype, mount.source, str(mount.point), str(mount.fs_root),
            _digest(mount.raw), _digest(tuple(m.raw for m in filesystem._mounts)))

    def _validate_record(self, evidence: FileIdentityEvidence, manifest: ExecutionManifest,
                         action: ManifestAction, index: int, manifest_sha256: str) -> None:
        if (evidence.schema != _SCHEMA or evidence.version != _VERSION
                or evidence.manifest_id != manifest.manifest_id or evidence.manifest_sha256 != manifest_sha256
                or evidence.action != action or evidence.action_index != index):
            raise IdentityEvidenceRejected('STOP: evidence version/manifest/action binding mismatch')
        binding, mount, filesystem = evidence.binding, self._mount, self._filesystem
        if (binding.root != str(self.root) or binding.root_identity != DirectoryIdentity(*filesystem._root_identity)
                or (binding.mount_id, binding.mount_fstype, binding.mount_source,
                    binding.mount_point, binding.mount_fs_root, binding.mount_record_sha256, binding.mount_table_sha256)
                != (mount.mount_id, mount.fstype, mount.source, str(mount.point), str(mount.fs_root),
                    _digest(mount.raw), _digest(tuple(m.raw for m in filesystem._mounts)))
                or any(parent.st_dev != binding.root_identity.st_dev or parent.st_ino <= 0
                    for parent in (binding.source_parent, binding.target_parent))):
            raise IdentityEvidenceRejected('STOP: evidence scratch root/parent/mount binding mismatch')
        value, content = evidence.filesystem, evidence.content
        _safe_file(value)
        if (value.st_dev != binding.root_identity.st_dev
                or (value.size, value.mtime_ns) != (action.expected_size, action.expected_mtime_ns)
                or content.algorithm != 'sha256' or content.coverage != 'FULL'
                or content.bytes_hashed != value.size or re.fullmatch('[0-9a-f]{64}', content.digest) is None):
            raise IdentityEvidenceRejected('STOP: invalid filesystem/full-content evidence')

    def _check_artifact_parent(self, parent: int) -> None:
        journal._check_parent(self.evidence_path, parent)
        fs._check_mounts(self.evidence_path.parent, parent, self._filesystem._mounts)
        if (os.fstat(parent).st_dev != self._filesystem._root_identity[0]
                or fs._mount_id(parent) != self._filesystem._mount_id
                or fs._read_mounts() != self._filesystem._mounts):
            raise IdentityEvidenceRejected('STOP: evidence parent mount binding changed')

    def _check_artifact(self, fd: int, parent: int) -> os.stat_result:
        self._check_artifact_parent(parent)
        value = journal._check_file(fd, parent, self.evidence_path.name)
        if (value.st_dev != self._filesystem._root_identity[0]
                or fs._mount_id(fd) != self._filesystem._mount_id
                or fs._read_mounts() != self._filesystem._mounts):
            raise IdentityEvidenceRejected('STOP: evidence mount binding changed')
        return value

    def capture(self, manifest: ExecutionManifest, action: ManifestAction, *,
                action_index: int) -> FileIdentityEvidence:
        if os.getpid() != self._pid:
            raise IdentityEvidenceRejected('STOP: inherited identity session')
        with self._mutex:
            if self._attempted or self._closed or self._poisoned:
                raise IdentityEvidenceRejected('STOP: capture unavailable; no retry')
            self._attempted = True
            storage_attempted = False
            try:
                envelope = self._bind(manifest, action, action_index)
                with self._filesystem.observe(manifest, action) as observation:
                    before, content = _hash_file(observation._source_fd)
                    observation._check(self._filesystem, manifest, action)
                    pin = os.dup(observation._source_fd)
                    self._resources.enter_context(fs._held_evidence_fd(pin))
                    self._pin_fd = pin
                    for attribute, held in (('_source_parent_fd', observation._source_parent),
                            ('_target_parent_fd', observation._target_parent)):
                        parent_pin = os.dup(held)
                        self._resources.enter_context(fs._held_evidence_fd(parent_pin))
                        setattr(self, attribute, parent_pin)
                    evidence = FileIdentityEvidence(_SCHEMA, _VERSION, manifest.manifest_id, envelope,
                        action_index, action, self._binding(observation._source_parent, observation._target_parent),
                        before, content)
                    self._validate_record(evidence, manifest, action, action_index, envelope)
                    raw = _frame(evidence)
                    with ExitStack() as stack:
                        parent = journal._open_parent(self.evidence_path, manifest, (), ())
                        stack.enter_context(fs._held_evidence_fd(parent))
                        self._filesystem._check_root()
                        self._check_artifact_parent(parent)
                        storage_attempted = True
                        fd = os.open(self.evidence_path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL
                            | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
                        stack.enter_context(fs._held_evidence_fd(fd))
                        journal._lock(fd, True)
                        self._check_artifact(fd, parent)
                        _write_record(fd, raw)
                        os.fsync(fd)
                        os.fsync(parent)
                        info = self._check_artifact(fd, parent)
                        if info.st_size != len(raw) or os.pread(fd, _MAX_RECORD_BYTES + 1, 0) != raw:
                            raise IdentityEvidenceReconciliationRequired('STOP: evidence changed during persistence')
                        after, repeated = _hash_file(observation._source_fd)
                        if (before, content) != (after, repeated):
                            raise IdentityEvidenceReconciliationRequired('STOP: source changed after evidence persistence')
                        observation._check(self._filesystem, manifest, action)
                        fs._check_source(observation._source_fd, observation._source_parent,
                            fs._locator(action.source)[-1], action, observation._before)
                        fs._absent(observation._target_parent, fs._locator(action.target)[-1])
                        if (before != _file_stat(os.fstat(observation._source_fd))
                                or _file_stat(info) != _file_stat(self._check_artifact(fd, parent))
                                or os.pread(fd, _MAX_RECORD_BYTES + 1, 0) != raw):
                            raise IdentityEvidenceReconciliationRequired('STOP: source/evidence changed after final hash')
                        # Independent read-only open: duplicating the writer fd
                        # would retain its exclusive flock and block later loads.
                        artifact_pin = os.open(self.evidence_path.name, fs._FILE, dir_fd=parent)
                        self._resources.enter_context(fs._held_evidence_fd(artifact_pin))
                        self._artifact_fd = artifact_pin
                        parent_pin = os.dup(parent)
                        self._resources.enter_context(fs._held_evidence_fd(parent_pin))
                        self._artifact_parent_fd = parent_pin
                        if (_file_stat(self._check_artifact(artifact_pin, parent_pin)) != _file_stat(info)
                                or os.pread(artifact_pin, _MAX_RECORD_BYTES + 1, 0) != raw):
                            raise IdentityEvidenceReconciliationRequired('STOP: artefact changed while pinning capture')
                # Both source checks, barriers and descriptor cleanup acknowledged.
                self._captured_record, self._artifact_stat = evidence, _file_stat(info)
                self._durable = True
                return evidence
            except BaseException as exc:
                self._poisoned = self._filesystem._poisoned = True
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    exc.add_note('E3A STOP: capture interrupted; no retry; preserve evidence bytes')
                    raise
                # After artefact creation even a plain rejection leaves bytes whose
                # acknowledgement/source stability is uncertain.
                if isinstance(exc, IdentityEvidenceRejected) and (not storage_attempted
                        or isinstance(exc, IdentityEvidenceReconciliationRequired)):
                    raise
                if isinstance(exc, Exception):
                    category = IdentityEvidenceReconciliationRequired if storage_attempted else IdentityEvidenceRejected
                    raise category('STOP: identity capture failed; preserve evidence bytes; no retry') from exc
                raise

    def load(self, manifest: ExecutionManifest, action: ManifestAction, *,
             action_index: int) -> FileIdentityEvidence:
        if os.getpid() != self._pid:
            raise IdentityEvidenceRejected('STOP: inherited identity session')
        with self._mutex:
            try:
                return self._load_record(manifest, action, action_index)
            except BaseException:
                # Later diagnostic loads cannot erase a previously observed STOP.
                self._poisoned = self._filesystem._poisoned = True
                raise

    def _load_record(self, manifest, action, index) -> FileIdentityEvidence:
        envelope = self._bind(manifest, action, index)
        try:
            with ExitStack() as stack:
                parent = journal._open_parent(self.evidence_path, manifest, (), ())
                stack.enter_context(fs._held_evidence_fd(parent))
                fd = os.open(self.evidence_path.name, fs._FILE, dir_fd=parent)
                stack.enter_context(fs._held_evidence_fd(fd))
                journal._lock(fd, False)
                before = _file_stat(self._check_artifact(fd, parent))
                raw = os.pread(fd, _MAX_RECORD_BYTES + 1, 0)
                if before.size > _MAX_RECORD_BYTES or len(raw) != before.size:
                    raise IdentityEvidenceRejected('STOP: oversized or incomplete evidence read')
                evidence = _decode_frame(raw)
                self._validate_record(evidence, manifest, action, index, envelope)
                if self._captured_record is not None:
                    if evidence != self._captured_record or before != self._artifact_stat:
                        raise IdentityEvidenceReconciliationRequired('STOP: acknowledged capture artefact changed')
                    if not self._closed and (_directory(parent) != _directory(self._artifact_parent_fd)
                            or _file_stat(self._check_artifact(self._artifact_fd, parent)) != before):
                        raise IdentityEvidenceReconciliationRequired('STOP: original capture artefact/parent replaced')
                if before != _file_stat(self._check_artifact(fd, parent)):
                    raise IdentityEvidenceReconciliationRequired('STOP: evidence changed during load')
            # Parseability does not prove a crashed writer's fsync acknowledgement.
            return evidence
        except (ValueError, OSError) as exc:
            if isinstance(exc, IdentityEvidenceRejected):
                raise
            raise IdentityEvidenceRejected('STOP: evidence cannot be safely loaded') from exc

    def _current_root(self, stack: ExitStack) -> int:
        root = fs._open_absolute(self.root)
        stack.enter_context(fs._held_evidence_fd(root))
        if (_directory(root) != DirectoryIdentity(*self._filesystem._root_identity)
                or fs._mount_id(root) != self._filesystem._mount_id
                or fs._read_mounts() != self._filesystem._mounts):
            raise IdentityEvidenceRejected('STOP: original root/mount binding unavailable')
        fs._check_mounts(self.root, root, self._filesystem._mounts)
        return root

    def _current_parent(self, root: int, parts: tuple[str, ...], stack: ExitStack,
                        expected: DirectoryIdentity) -> int:
        fd = root
        for part in parts[:-1]:
            fd = os.open(part, fs._DIRECTORY, dir_fd=fd)
            stack.enter_context(fs._held_evidence_fd(fd))
            fs._secure_directory(fd)
            if (os.fstat(fd).st_dev != expected.st_dev
                    or fs._mount_id(fd) != self._filesystem._mount_id):
                raise IdentityEvidenceRejected('STOP: parent mount crossing')
        if _directory(fd) != expected:
            raise IdentityEvidenceRejected('STOP: original action parent replaced')
        return fd

    def _locator_comparison(self, locator: str, parent: int, evidence: FileIdentityEvidence,
                            stack: ExitStack) -> tuple[LocatorComparison, int | None]:
        name = fs._locator(locator)[-1]
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return LocatorComparison(locator, 'ABSENT', None, None, 'UNKNOWN', 'UNKNOWN', 'UNKNOWN', ()), None
        current = _file_stat(info)
        _safe_file(current)  # Never open/hash symlinks, multiple hardlinks or special files.
        fd = os.open(name, fs._FILE, dir_fd=parent)
        stack.enter_context(fs._held_evidence_fd(fd))
        if fs._mount_id(fd) != evidence.binding.mount_id:
            raise IdentityEvidenceRejected('STOP: file mount crossing')
        observed, content = _hash_file(fd)
        if current != observed or _file_stat(os.stat(name, dir_fd=parent, follow_symlinks=False)) != observed:
            raise IdentityEvidenceReconciliationRequired('STOP: locator changed during comparison')
        # Rename legitimately changes ctime. It only detects mutations within a read.
        expected = evidence.filesystem
        attributes = ('st_dev', 'st_ino', 'size', 'mtime_ns', 'file_type', 'nlink', 'mode', 'uid', 'gid')
        numeric_match = all(getattr(observed, key) == getattr(expected, key) for key in attributes)
        physical = 'UNKNOWN'
        if not self._closed and self._pin_fd is not None:
            pin = _file_stat(os.fstat(self._pin_fd))
            if (pin.st_dev, pin.st_ino) != (expected.st_dev, expected.st_ino):
                raise IdentityEvidenceRejected('STOP: live file pin binding changed')
            physical = 'MATCH' if (observed.st_dev, observed.st_ino) == (pin.st_dev, pin.st_ino) else 'MISMATCH'
        return LocatorComparison(locator, 'PRESENT', observed, content,
            'MATCH' if numeric_match else 'MISMATCH',
            'MATCH' if content == evidence.content else 'MISMATCH', physical, ()), fd

    def compare(self, manifest: ExecutionManifest, action: ManifestAction, *,
                action_index: int) -> IdentityComparison:
        """Reobserve both locators; statuses are diagnostics, never continuation."""
        if os.getpid() != self._pid:
            raise IdentityEvidenceRejected('STOP: inherited identity session')
        with self._mutex:
            try:
                evidence = self._load_record(manifest, action, action_index)
                if self._poisoned or not self._durable:
                    raise IdentityEvidenceReconciliationRequired('STOP: no acknowledged durable capture in this context')
                with ExitStack() as stack:
                    root = self._current_root(stack)
                    source_parts, target_parts = fs._locator(action.source), fs._locator(action.target)
                    source_parent = self._current_parent(root, source_parts, stack, evidence.binding.source_parent)
                    target_parent = self._current_parent(root, target_parts, stack, evidence.binding.target_parent)
                    if not self._closed:
                        if (_directory(self._source_parent_fd) != evidence.binding.source_parent
                                or _directory(self._target_parent_fd) != evidence.binding.target_parent):
                            raise IdentityEvidenceRejected('STOP: live parent pin binding changed')
                    source, source_fd = self._locator_comparison(action.source, source_parent, evidence, stack)
                    target, target_fd = self._locator_comparison(action.target, target_parent, evidence, stack)
                    # A hash/absence observation can become stale before the other read.
                    for observed, fd, parent in ((source, source_fd, source_parent), (target, target_fd, target_parent)):
                        name = fs._locator(observed.locator)[-1]
                        if fd is None:
                            fs._absent(parent, name)
                        elif (_file_stat(os.fstat(fd)) != observed.observed_file
                                or _file_stat(os.stat(name, dir_fd=parent, follow_symlinks=False)) != observed.observed_file):
                            raise IdentityEvidenceReconciliationRequired('STOP: observation changed before comparison completed')
                    fresh = self._current_root(stack)
                    self._current_parent(fresh, source_parts, stack, evidence.binding.source_parent)
                    self._current_parent(fresh, target_parts, stack, evidence.binding.target_parent)
                    if self._load_record(manifest, action, action_index) != evidence:
                        raise IdentityEvidenceReconciliationRequired('STOP: evidence changed during comparison')
                present = [item for item in (source, target) if item.presence == 'PRESENT']
                outcome, diagnostics = 'UNKNOWN', ('STOP: no unique source/target state',)
                if len(present) == 1:
                    current = present[0]
                    metadata = ('size', 'mtime_ns', 'file_type', 'nlink', 'mode', 'uid', 'gid')
                    if (current.content_match == 'MISMATCH' or current.physical_identity_match == 'MISMATCH'
                            or any(getattr(current.observed_file, key) != getattr(evidence.filesystem, key) for key in metadata)):
                        outcome, diagnostics = 'MISMATCH', ('STOP: observed file contradicts identity evidence',)
                    elif current.physical_identity_match == current.filesystem_match == current.content_match == 'MATCH':
                        outcome, diagnostics = 'MATCH', ('live held inode and full content match; no execution authority',)
                    else:
                        diagnostics = ('STOP: content equality cannot prove original inode after live evidence loss',)
                if outcome != 'MATCH':
                    self._poisoned = self._filesystem._poisoned = True
                return IdentityComparison(outcome, source, target, diagnostics)
            except (KeyboardInterrupt, SystemExit) as exc:
                self._poisoned = self._filesystem._poisoned = True
                exc.add_note('E3A STOP: comparison interrupted; no retry/resume')
                raise
            except Exception as exc:
                self._poisoned = self._filesystem._poisoned = True
                return IdentityComparison('UNKNOWN', None, None, (f'STOP: {type(exc).__name__}: {exc}',))

    def close(self) -> None:
        self.__exit__(None, None, None)

    def __enter__(self) -> ScratchIdentitySession:
        if self._closed or os.getpid() != self._pid:
            raise IdentityEvidenceRejected('STOP: identity session unavailable')
        return self

    def __exit__(self, _type, primary, traceback) -> None:
        if os.getpid() != self._pid:
            if not self._closed:
                self._closed = True
                self._resources.__exit__(_type, primary, traceback)
            return
        with self._mutex:
            if not self._closed:
                self._closed = True
                self._resources.__exit__(_type, primary, traceback)
