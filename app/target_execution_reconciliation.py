"""E4A read-only inspection of existing E1–E3B scratch artefacts after restart.

Every result is STOP for manual evaluation. No live inode pin survives restart;
valid bytes, numeric metadata and content equality grant no execution authority.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re

from . import target_execution_filesystem as fs
from . import target_execution_identity as identity
from . import target_execution_journal as journal
from .target_execution_manifest import ExecutionManifest, ManifestAction, JournalEntry, manifest_to_json


@dataclass(frozen=True)
class JournalInspection:
    status: str
    manifest_binding_valid: bool | None = None
    entries: tuple[JournalEntry, ...] = ()
    last_action_phase: str | None = None
    diagnostics: tuple[str, ...] = ()
    history_completeness: str = 'UNKNOWN'


@dataclass(frozen=True)
class EvidenceInspection:
    status: str
    record: identity.FileIdentityEvidence | None = None
    diagnostics: tuple[str, ...] = ()
    capture_acknowledgement: str = 'UNKNOWN'


@dataclass(frozen=True)
class ScratchRestartInspection:
    journal: JournalInspection
    evidence: EvidenceInspection
    source: identity.LocatorComparison | None
    target: identity.LocatorComparison | None
    observed_state: str
    stop_reasons: tuple[str, ...]
    contradictions: tuple[str, ...]
    diagnostics: tuple[str, ...]
    outcome: str = 'STOP'
    manual_review_required: bool = True
    identity_continuity: str = 'UNKNOWN'
    manifest_id: str | None = None
    action_id: str | None = None
    action_index: int | None = None
    scratch_root: str | None = None


def _diagnostic(exc: Exception) -> str:
    return f'STOP: {type(exc).__name__}: {exc}'


def _paths(manifest, action, index, root, journal_path, evidence_path) -> None:
    # This gate precedes validators that lstat the manifest's root. Never resolve
    # caller-supplied symlinks or a production root to find out whether it is safe.
    fs._absolute(root)
    if (not root.is_relative_to('/tmp') or root.parent == Path('/tmp')
            or re.fullmatch(r'v6-e2a-[0-9a-f]{32}', root.name) is None):
        raise ValueError('existing E2A scratch root in a private /tmp directory required')
    if (type(manifest) is not ExecutionManifest or type(action) is not ManifestAction
            or manifest.payload.library_root != str(root)):
        raise ValueError('immutable manifest must name this scratch root')
    if (type(index) is not int or not 0 <= index < len(manifest.payload.actions)
            or action != manifest.payload.actions[index] or action.action not in {'MOVE', 'QUARANTINE'}):
        raise ValueError('exact sealed MOVE/QUARANTINE action/index required')
    if (journal_path != root.parent / (root.name + '.journal.jsonl')
            or evidence_path != root.parent / (root.name + '.identity.json')):
        raise ValueError('original adjacent E3B journal and identity paths required')


def _open_artifact(path, parent, mount_id, stack, anchors):
    fd = os.open(path.name, fs._FILE, dir_fd=parent)
    stack.enter_context(fs._held_evidence_fd(fd))
    info = journal._check_file(fd, parent, path.name)
    if fs._mount_id(fd) != mount_id:
        raise ValueError('artefact mount crossing')
    journal._lock(fd, False)
    anchors.append((path, fd, identity._file_stat(info)))
    return fd, info


def _journal(path, parent, mount_id, stack, anchors, manifest, index):
    entries, binding = (), None
    try:
        fd, _ = _open_artifact(path, parent, mount_id, stack, anchors)
        try:
            entries = journal._read_history(fd, manifest)
            binding, status, diagnostics = True, 'VALID', ()
        except journal.ReconciliationRequired as exc:
            entries = exc.entries
            binding = True if exc.header_valid else None
            status, diagnostics = 'INVALID', (_diagnostic(exc),)
        phase = next((e.state for e in reversed(entries) if e.action_index == index), None)
        return JournalInspection(status, binding, entries, phase, diagnostics)
    except (OSError, ValueError) as exc:
        return JournalInspection('UNAVAILABLE', diagnostics=(_diagnostic(exc),))


def _evidence(path, parent, mount, mounts, root, root_fd, stack, anchors, manifest, action, index):
    opened = False
    try:
        fd, info = _open_artifact(path, parent, mount.mount_id, stack, anchors)
        opened = True
        raw = os.pread(fd, identity._MAX_RECORD_BYTES + 1, 0)
        if info.st_size > identity._MAX_RECORD_BYTES or len(raw) != info.st_size:
            raise ValueError('oversized or incomplete identity evidence read')
        record = identity._decode_frame(raw)
        envelope = hashlib.sha256(manifest_to_json(manifest).encode('utf-8')).hexdigest()
        identity._validate_record(record, manifest, action, index, envelope,
            root=root, root_identity=identity._directory(root_fd), mount=mount, mounts=mounts)
        return EvidenceInspection('VALID', record)
    except (OSError, ValueError) as exc:
        return EvidenceInspection('INVALID' if opened else 'UNAVAILABLE', diagnostics=(_diagnostic(exc),))


def _observe(locator, expected_parent, root_fd, mount_id, record, stack):
    parent, current = None, None
    try:
        parent = identity._current_parent(root_fd, fs._locator(locator), stack, expected_parent, mount_id)
        try:
            current = identity._file_stat(os.stat(fs._locator(locator)[-1], dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
            pass
        if current is not None:
            try:
                identity._safe_file(current)
            except identity.IdentityEvidenceRejected as exc:
                return identity.LocatorComparison(locator, 'UNSAFE', current, None,
                    'UNKNOWN', 'UNKNOWN', 'UNKNOWN', (_diagnostic(exc),)), None, parent
        comparison, fd = identity._locator_comparison(locator, parent, record, stack, mount_id)
        return comparison, fd, parent
    except (OSError, ValueError) as exc:
        return identity.LocatorComparison(locator, 'UNKNOWN', current, None,
            'UNKNOWN', 'UNKNOWN', 'UNKNOWN', (_diagnostic(exc),)), None, parent


def _state(source, target):
    states = (source.presence, target.presence)
    return {('PRESENT', 'ABSENT'): 'SOURCE_ONLY', ('ABSENT', 'PRESENT'): 'TARGET_ONLY',
        ('PRESENT', 'PRESENT'): 'BOTH_PRESENT', ('ABSENT', 'ABSENT'): 'BOTH_ABSENT'}.get(states,
            'UNSAFE' if 'UNSAFE' in states else 'UNKNOWN')


def inspect_scratch_action(manifest: ExecutionManifest, action: ManifestAction, *,
                           action_index: int, scratch_root: Path, journal_path: Path,
                           evidence_path: Path) -> ScratchRestartInspection:
    """Inspect existing scratch inputs; never create an execution context or write.

    VALID means the observed bytes pass strict validation. A valid prefix cannot
    prove history completeness or fsync acknowledgement. MATCH compares observed
    content/numbers only; physical identity continuity is always UNKNOWN here.
    Caller must retain exclusive ownership of scratch data, as with E2A/E3A.
    """
    history, evidence = JournalInspection('NOT_INSPECTED'), EvidenceInspection('NOT_INSPECTED')
    source = target = None
    observed = 'UNKNOWN'
    reasons = ['IDENTITY_CONTINUITY_UNPROVEN']
    contradictions = []
    diagnostics = [
        'STOP: restart lost the original live inode pin; matching SHA-256 or dev/inode does not prove identity continuity',
        'Journal phases are recorded assertions only: PENDING does not prove rename; FS_APPLIED does not prove DB updates; FS_VERIFIED does not prove COMPLETE',
        'History completeness, writer/capture fsync acknowledgement and DB state are unproven; manual evaluation required',
        'Checksums detect damaged bytes, not deliberate replacement of the original capture record after its live anchor was lost',
    ]
    try:
        root, journal_path, evidence_path = Path(scratch_root), Path(journal_path), Path(evidence_path)
        _paths(manifest, action, action_index, root, journal_path, evidence_path)
        mounts = fs._read_mounts()
        # Reject a known remote/special mount on the path before opening it.
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
            if root_identity.st_dev != os.fstat(parent).st_dev or fs._mount_id(parent) != mount.mount_id:
                raise ValueError('scratch root/artefact parent mount crossing')
            mount_identity = f'{mount.fstype}:{mount.source}:{root_identity.st_dev}:{root_identity.st_ino}'
            fs._validate_scratch_action(manifest, action, root, mount_identity)
            anchors = []
            history = _journal(journal_path, parent, mount.mount_id, stack, anchors, manifest, action_index)
            evidence = _evidence(evidence_path, parent, mount, mounts, root, root_fd,
                stack, anchors, manifest, action, action_index)
            record = evidence.record
            source, source_fd, source_parent = _observe(action.source,
                record.binding.source_parent if record else None, root_fd, mount.mount_id, record, stack)
            target, target_fd, target_parent = _observe(action.target,
                record.binding.target_parent if record else None, root_fd, mount.mount_id, record, stack)
            observed = _state(source, target)

            # Keep shared artifact locks and read descriptors until observations
            # and path bindings have all been checked again. No fsync or repair.
            for item, fd, held_parent in ((source, source_fd, source_parent), (target, target_fd, target_parent)):
                if item.presence not in {'PRESENT', 'ABSENT'}:
                    continue
                if item.presence == 'ABSENT':
                    fs._absent(held_parent, fs._locator(item.locator)[-1])
                elif (identity._file_stat(os.fstat(fd)) != item.observed_file
                        or identity._file_stat(os.stat(fs._locator(item.locator)[-1],
                            dir_fd=held_parent, follow_symlinks=False)) != item.observed_file):
                    raise ValueError('locator changed before inspection completed')
                identity._current_parent(root_fd, fs._locator(item.locator), stack,
                    identity._directory(held_parent), mount.mount_id)
            for path, fd, before in anchors:
                if identity._file_stat(journal._check_file(fd, parent, path.name)) != before:
                    raise ValueError('artefact changed during inspection')
                journal._check_parent(path, parent)
            fresh = fs._open_absolute(root)
            stack.enter_context(fs._held_evidence_fd(fresh))
            if (identity._directory(fresh) != root_identity or fs._mount_id(fresh) != mount.mount_id
                    or fs._read_mounts() != mounts):
                raise ValueError('scratch root/mount changed during inspection')
            fs._check_mounts(root, fresh, mounts)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        reasons.append('INSPECTION_UNCERTAIN')
        diagnostics.append(_diagnostic(exc))
        # Retain observations as diagnostics, but revoke any stable-state claim.
        observed = 'UNKNOWN'
        history = replace(history, status='UNTRUSTED') if history.status == 'VALID' else history
        evidence = replace(evidence, status='UNTRUSTED') if evidence.status == 'VALID' else evidence

    for label, item in (('JOURNAL', history), ('EVIDENCE', evidence)):
        diagnostics.extend(item.diagnostics)
        if item.status != 'VALID':
            reasons.append(label + '_UNTRUSTED')
    for item in (source, target):
        if item is None:
            continue
        diagnostics.extend(item.diagnostics)
        if item.presence in {'UNKNOWN', 'UNSAFE'}:
            reasons.append('UNSAFE_LOCATOR')
        if item.content_match == 'MISMATCH':
            contradictions.append('CONTENT_MISMATCH')
        if item.filesystem_match == 'MISMATCH':
            contradictions.append('METADATA_MISMATCH')
    if observed == 'BOTH_PRESENT':
        reasons.append('SOURCE_AND_TARGET_PRESENT')
    elif observed == 'BOTH_ABSENT':
        reasons.append('SOURCE_AND_TARGET_ABSENT')
    if history.last_action_phase == 'PENDING' and observed == 'TARGET_ONLY':
        contradictions.append('PENDING_WITH_TARGET_ONLY')
    if history.last_action_phase in {'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'} and observed != 'TARGET_ONLY':
        contradictions.append('RECORDED_FS_PROGRESS_WITHOUT_TARGET')
    reasons.extend(contradictions)
    return ScratchRestartInspection(history, evidence, source, target, observed,
        tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(contradictions)), tuple(diagnostics),
        manifest_id=manifest.manifest_id if type(manifest) is ExecutionManifest else None,
        action_id=action.action_id if type(action) is ManifestAction else None,
        action_index=action_index if type(action_index) is int else None, scratch_root=str(scratch_root))
