"""E5B: read-only, single Video MOVE diagnostics on original E5A scratch data.

No execution session, recovery, copy, locator update or acknowledgement is
created. Callers must retain E4A's exclusive ownership of the scratch tree;
these checks do not create a maintenance boundary against hostile same-UID
path replacement. SQLite keeps its normal locking on the mutable original.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass, replace
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote

from . import target_execution_filesystem as fs
from . import target_execution_identity as identity
from . import target_execution_journal as journal
from . import target_execution_reconciliation as e4a
from .target_execution_manifest import DatabaseFingerprint, ExecutionManifest, LocatorPatch, ManifestAction


@dataclass(frozen=True)
class VideoLocators:
    relative_path: str
    root_folder: str


@dataclass(frozen=True)
class SQLiteArtifact:
    suffix: str
    presence: str
    observed: identity.FileStatEvidence | None = None
    content: identity.ContentEvidence | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class SQLiteInspection:
    path: str | None
    status: str = 'DB_UNINSPECTED'
    locator_state: str = 'UNINSPECTED'
    expected_video_id: int | None = None
    video_id: int | None = None
    current: VideoLocators | None = None
    expected_old: VideoLocators | None = None
    expected_new: VideoLocators | None = None
    approved_patches: tuple[LocatorPatch, ...] = ()
    expected_size: int | None = None
    expected_mtime_ns: int | None = None
    observed_size: int | None = None
    observed_mtime_ns: int | None = None
    artifacts: tuple[SQLiteArtifact, ...] = ()
    header_hex: str | None = None
    original_fingerprint_match: bool | None = None
    contradictions: tuple[str, ...] = ()
    stop_reasons: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    transaction_state: str = 'UNKNOWN'
    full_post_state: str = 'UNKNOWN'
    limitations: tuple[str, ...] = (
        'Locator and size/mtime checks do not prove that all other DB values are unchanged or that the full approved DB post-state exists.',
        'Sidecar presence alone does not prove a hot journal or an uncommitted transaction; original commit acknowledgement remains UNKNOWN.',
    )


@dataclass(frozen=True)
class ScratchVideoDiagnosis:
    manifest_id: str | None
    action_id: str | None
    action_index: int | None
    filesystem: e4a.ScratchRestartInspection
    database: SQLiteInspection
    contradictions: tuple[str, ...]
    stop_reasons: tuple[str, ...]
    diagnostics: tuple[str, ...]
    outcome: str = 'STOP'
    manual_review_required: bool = True
    identity_continuity: str = 'UNKNOWN'


def _scope(manifest, action, index, root, journal_path, evidence_path):
    e4a._paths(manifest, action, index, root, journal_path, evidence_path)
    if (type(index) is not int or index != 0 or manifest.payload.actions != (action,)
            or action.action != 'MOVE' or action.object_kind != 'video'
            or type(action.object_id) is not int or action.object_id <= 0
            or manifest.payload.expected_post_hash is None):
        raise ValueError('one sealed Video MOVE at index 0 with approved post-state hash required')
    patches = manifest.payload.locator_patches
    if any(p.object_kind != 'video' or p.object_id != action.object_id
            or p.field not in {'relative_path', 'root_folder'} for p in patches):
        raise ValueError('patch outside the single Video locator scope')
    fields = {p.field: p for p in patches}
    new = VideoLocators(action.target, action.target.split('/')[0])
    # An absent root_folder patch means it was approved to stay at its target
    # value. Do not infer the original DB root_folder from a source directory.
    old = VideoLocators(action.source, fields['root_folder'].before if 'root_folder' in fields else new.root_folder)
    required = tuple(LocatorPatch('video', action.object_id, name, before, after)
        for name, before, after in (('relative_path', old.relative_path, new.relative_path),
            ('root_folder', old.root_folder, new.root_folder)) if before != after)
    if len(patches) != len(required) or set(patches) != set(required):
        raise ValueError('exact approved old/target locator patches required')
    return old, new


def _artifact(path, suffix, parent, mount_id, stack, anchors):
    name = path.name + suffix
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return SQLiteArtifact(suffix, 'ABSENT')
    except OSError as exc:
        return SQLiteArtifact(suffix, 'UNKNOWN', diagnostics=(e4a._diagnostic(exc),))
    observed = identity._file_stat(info)
    try:
        identity._safe_file(observed)
        if not observed.mode & 0o400:
            raise ValueError('scratch artifact is not owner-readable')
        fd, _ = e4a._open_artifact(path.with_name(name), parent, mount_id, stack, anchors)
        current, content = identity._hash_file(fd)
        if current != observed:
            raise ValueError('artifact changed while opening')
        return SQLiteArtifact(suffix, 'PRESENT', observed, content)
    except (OSError, ValueError) as exc:
        return SQLiteArtifact(suffix, 'PRESENT', observed, diagnostics=(e4a._diagnostic(exc),))


def _read_values(connection, action, report):
    if connection.execute('PRAGMA journal_mode').fetchone() != ('delete',):
        raise ValueError('only original DELETE-mode scratch SQLite is inspectable')
    if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
        raise ValueError('SQLite structural integrity check failed')
    if connection.execute("SELECT type FROM sqlite_master WHERE name='videos'").fetchall() != [('table',)]:
        raise ValueError('original videos table required')
    rows = connection.execute('SELECT id,relative_path,root_folder,size,mtime_ns FROM videos '
        'WHERE id=? OR relative_path IN (?,?)', (action.object_id, action.source, action.target)).fetchall()
    selected = [row for row in rows if type(row[0]) is int and row[0] == action.object_id]
    contradictions = []
    if len(selected) != 1:
        contradictions.append('EXPECTED_VIDEO_ID_MISSING_OR_AMBIGUOUS')
    for row in rows:
        if row[0] != action.object_id:
            contradictions.append('LOCATOR_OWNED_BY_OTHER_VIDEO')
    if len(selected) != 1:
        return replace(report, status='DB_INSPECTED', locator_state='MISMATCH',
            contradictions=tuple(dict.fromkeys(contradictions)))
    video_id, relative_path, root_folder, size, mtime = selected[0]
    current = VideoLocators(relative_path, root_folder)
    if (size, mtime) != (action.expected_size, action.expected_mtime_ns):
        contradictions.append('VIDEO_SIZE_MTIME_PRECONDITION_MISMATCH')
    if current not in (report.expected_old, report.expected_new):
        contradictions.append('VIDEO_LOCATORS_OUTSIDE_APPROVED_PATCHES')
    state = ('MISMATCH' if contradictions else
        'OLD_VALUES' if current == report.expected_old else 'NEW_VALUES')
    return replace(report, status='DB_INSPECTED', locator_state=state, video_id=video_id,
        current=current, observed_size=size, observed_mtime_ns=mtime,
        contradictions=tuple(dict.fromkeys(contradictions)))


def _inspect_database(manifest, action, index, root, journal_path, evidence_path, filesystem):
    # No caller-provided DB path is accepted, even by this private helper.
    path = root.parent / (root.name + '.sqlite')
    report = SQLiteInspection(str(path))
    attempted = read = False
    try:
        old, new = _scope(manifest, action, index, root, journal_path, evidence_path)
        report = replace(report, expected_old=old, expected_new=new, expected_video_id=action.object_id,
            approved_patches=manifest.payload.locator_patches,
            expected_size=action.expected_size, expected_mtime_ns=action.expected_mtime_ns)
        mounts = fs._read_mounts()
        if any(root.is_relative_to(m.point) and m.fstype not in {'ext4', 'btrfs', 'overlay'} for m in mounts):
            raise ValueError('nonlocal or unsupported scratch mount')
        with ExitStack() as stack:
            parent = fs._open_absolute(root.parent)
            stack.enter_context(fs._held_evidence_fd(parent))
            fs._secure_directory(parent)
            fs._check_mounts(root.parent, parent, mounts)
            root_fd = os.open(root.name, fs._DIRECTORY, dir_fd=parent)
            stack.enter_context(fs._held_evidence_fd(root_fd))
            fs._secure_directory(root_fd)
            mount = fs._check_mounts(root, root_fd, mounts)
            root_identity = identity._directory(root_fd)
            parent_before = identity._file_stat(os.fstat(parent))
            if root_identity.st_dev != parent_before.st_dev or fs._mount_id(parent) != mount.mount_id:
                raise ValueError('scratch root/DB parent mount crossing')
            fs._validate_scratch_action(manifest, action, root,
                f'{mount.fstype}:{mount.source}:{root_identity.st_dev}:{root_identity.st_ino}')
            anchors = []
            execution_artifacts = tuple((p, _artifact(p, '', parent, mount.mount_id, stack, anchors))
                for p in (journal_path, evidence_path))
            artifacts = tuple(_artifact(path, suffix, parent, mount.mount_id, stack, anchors)
                for suffix in ('', '-journal', '-wal', '-shm'))
            report = replace(report, artifacts=artifacts)
            database = artifacts[0]
            # Sidecar information is useful even when the main DB is unavailable.
            sidecars = any(a.presence != 'ABSENT' for a in artifacts[1:])
            reasons = ['SQLITE_SIDECAR'] if sidecars else []
            if database.presence != 'PRESENT' or database.content is None:
                reasons.append('SQLITE_UNSAFE_OR_UNAVAILABLE')
            db_fd = next((fd for p, fd, _ in anchors if p == path), None)
            if database.content is not None:
                header = os.pread(db_fd, 100, 0)
                report = replace(report, header_hex=header.hex())
                # Format version 1 is the E5A rollback-mode contract. WAL headers
                # must be rejected before SQLite can inspect/create any sidecars.
                if len(header) != 100 or header[:16] != b'SQLite format 3\0' or header[18:20] != b'\x01\x01':
                    reasons.append('SQLITE_UNSUPPORTED_HEADER')
            report = replace(report, stop_reasons=tuple(reasons), diagnostics=tuple(
                d for a in artifacts for d in a.diagnostics))

            def stable():
                journal._check_parent(path, parent)
                if identity._file_stat(os.fstat(parent)) != parent_before:
                    raise ValueError('scratch artifact parent changed during inspection')
                fresh = fs._open_absolute(root)
                with fs._held_evidence_fd(fresh):
                    if (identity._directory(fresh) != root_identity or fs._mount_id(fresh) != mount.mount_id
                            or fs._read_mounts() != mounts):
                        raise ValueError('scratch root/mount changed during inspection')
                    fs._check_mounts(root, fresh, mounts)
                for p, fd, before in anchors:
                    if identity._file_stat(journal._check_file(fd, parent, p.name)) != before:
                        raise ValueError('scratch artifact binding changed during inspection')
                    if fs._mount_id(fd) != mount.mount_id:
                        raise ValueError('scratch artifact mount changed')
                for artifact in artifacts:
                    if artifact.presence == 'ABSENT':
                        fs._absent(parent, path.name + artifact.suffix)
                    elif artifact.observed is not None:
                        observed = identity._file_stat(os.stat(path.name + artifact.suffix,
                            dir_fd=parent, follow_symlinks=False))
                        if observed != artifact.observed:
                            raise ValueError('SQLite artifact changed during inspection')
                for p, artifact in execution_artifacts:
                    if artifact.presence == 'ABSENT':
                        fs._absent(parent, p.name)
                    elif artifact.observed is not None and identity._file_stat(os.stat(
                            p.name, dir_fd=parent, follow_symlinks=False)) != artifact.observed:
                        raise ValueError('execution artifact changed during combined inspection')
                for artifact in artifacts:
                    if artifact.content is not None:
                        fd = next(fd for p, fd, _ in anchors if p.name == path.name + artifact.suffix)
                        if identity._hash_file(fd) != (artifact.observed, artifact.content):
                            raise ValueError('SQLite artifact bytes changed during inspection')

            stable()
            if not reasons:
                # No immutable=1, nolock=1 or alternative VFS on this original.
                # A read transaction retains SQLite's SHARED lock until all
                # values and path/hash/sidecar stability checks have completed.
                attempted = True
                with closing(sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro',
                        uri=True, timeout=0, isolation_level=None)) as connection:
                    connection.execute('PRAGMA query_only=ON')
                    connection.execute('PRAGMA trusted_schema=OFF')
                    connection.execute('BEGIN')
                    report = _read_values(connection, action, report)
                    version = connection.execute('PRAGMA user_version').fetchone()[0]
                    fingerprint = DatabaseFingerprint(database.content.digest, database.observed.size,
                        database.observed.mtime_ns, version)
                    report = replace(report, original_fingerprint_match=fingerprint == manifest.payload.db_fingerprint)
                    read = True
                    stable()
                # Closing the reader must not have created/deleted a sidecar.
                stable()
            final_fs = e4a.inspect_scratch_action(manifest, action, action_index=index,
                scratch_root=root, journal_path=journal_path, evidence_path=evidence_path)
            if final_fs != filesystem:
                raise ValueError('filesystem/journal/evidence changed during combined inspection')
            stable()
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
        reason = 'SQLITE_UNSTABLE' if read else 'SQLITE_READ_FAILED' if attempted else 'SQLITE_GUARD_FAILED'
        report = replace(report, status='DB_UNINSPECTED',
            locator_state='UNKNOWN' if attempted else 'UNINSPECTED',
            stop_reasons=(*report.stop_reasons, reason), diagnostics=(*report.diagnostics, e4a._diagnostic(exc)))
    return report


def inspect_scratch_video_move(manifest: ExecutionManifest, action: ManifestAction, *,
                              action_index: int, scratch_root: Path, journal_path: Path,
                              evidence_path: Path) -> ScratchVideoDiagnosis:
    """Diagnose one existing E5A scratch context. Always STOP; never recovery.

    Sidecars cause DB_UNINSPECTED without any SQLite connection to the original.
    OLD_VALUES/NEW_VALUES describe only approved locators and preconditions, not
    an acknowledged commit, full DB post-state or surviving inode continuity.
    """
    filesystem = e4a.inspect_scratch_action(manifest, action, action_index=action_index,
        scratch_root=scratch_root, journal_path=journal_path, evidence_path=evidence_path)
    try:
        database = _inspect_database(manifest, action, action_index, Path(scratch_root),
            Path(journal_path), Path(evidence_path), filesystem)
    except (TypeError, ValueError) as exc:
        database = SQLiteInspection(None, stop_reasons=('SQLITE_GUARD_FAILED',),
            diagnostics=(e4a._diagnostic(exc),))
    if 'SQLITE_UNSTABLE' in database.stop_reasons:
        # Retain raw observations for the human, but revoke a combined stable
        # snapshot claim when any of its inputs changed across the read.
        filesystem = replace(filesystem, observed_state='UNKNOWN',
            journal=replace(filesystem.journal, status='UNTRUSTED')
                if filesystem.journal.status == 'VALID' else filesystem.journal,
            evidence=replace(filesystem.evidence, status='UNTRUSTED')
                if filesystem.evidence.status == 'VALID' else filesystem.evidence,
            stop_reasons=(*filesystem.stop_reasons, 'INSPECTION_UNCERTAIN'))
    contradictions = [*filesystem.contradictions, *database.contradictions]
    state, values = filesystem.observed_state, database.locator_state
    if (state, values) in {('TARGET_ONLY', 'OLD_VALUES'), ('SOURCE_ONLY', 'NEW_VALUES')}:
        contradictions.append(state + '_WITH_' + values)
    if filesystem.journal.last_action_phase in {'DB_RECONCILED', 'COMPLETE'} and values == 'OLD_VALUES':
        contradictions.append('RECORDED_DB_PROGRESS_WITH_OLD_VALUES')
    reasons = [*filesystem.stop_reasons, *database.stop_reasons, *contradictions,
        'MANUAL_REVIEW_REQUIRED', 'FULL_DB_POST_STATE_UNPROVEN']
    if values == 'MISMATCH':
        reasons.append('DB_MISMATCH')
    if database.status == 'DB_UNINSPECTED':
        reasons.append('DB_UNINSPECTED')
    diagnostics = (*filesystem.diagnostics, *database.diagnostics,
        'Journal phases never prove current FS or DB state; parseable frames do not prove fsync acknowledgement.',
        'FS_VERIFIED is not DB_RECONCILED; DB_RECONCILED is not automatically COMPLETE.',
        *database.limitations)
    return ScratchVideoDiagnosis(filesystem.manifest_id, filesystem.action_id, filesystem.action_index,
        filesystem, database, tuple(dict.fromkeys(contradictions)), tuple(dict.fromkeys(reasons)), diagnostics)
