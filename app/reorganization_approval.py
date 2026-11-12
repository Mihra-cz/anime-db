"""Local canonical approval transport. This module has no execution capability.

The host inbox is immutable/read-only to the web. Only review/approval receipts
are web writable. Host acceptance uses an independent private create-once ledger.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import secrets

from . import target_execution_filesystem as fs
from . import target_execution_journal as journal
from . import target_execution_mounts as mounts
from .target_execution_manifest import (
    RuntimeEvidence, WarningAcknowledgement, manifest_from_json, manifest_to_json,
    preflight_execution_manifest, _strict_json_load, _decode_type,
)
from .tools.execution_manifest import _read_current, observe_runtime, validate_local_output

MAX_AGE_SECONDS = 900
MAX_RECORD_BYTES = 16 * 1024 * 1024


def require_current_proposal(bundle):
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(bundle['published_at'])).total_seconds()
    if not 0 <= age <= MAX_AGE_SECONDS:
        raise ValueError('host proposal expired; prepare a new proposal')


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def sha256(manifest):
    return hashlib.sha256(manifest_to_json(manifest).encode('utf-8')).hexdigest()


def identity(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('canonical SHA-256 required')
    return value


def record_path(directory, name):
    directory = fs._absolute(Path(directory))
    path = directory / name
    validate_local_output(path)
    return path


def read_record(directory, name):
    path = record_path(directory, name)
    with fs._held_evidence_fd(fs._open_absolute(path.parent)) as parent:
        fs._secure_directory(parent)
        with fs._held_evidence_fd(os.open(path.name, fs._FILE, dir_fd=parent)) as fd:
            before = journal._check_file(fd, parent, path.name)
            if before.st_size > MAX_RECORD_BYTES:
                raise ValueError('approval record too large')
            raw = os.pread(fd, before.st_size + 1, 0).decode('utf-8')
            after = journal._check_file(fd, parent, path.name)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('approval record changed while reading')
    return raw


def write_record(directory, name, raw):
    """O_EXCL across processes; partial/uncertain writes are retained as STOP."""
    path = record_path(directory, name)
    data = raw.encode('utf-8')
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError('approval record too large')
    with fs._held_evidence_fd(fs._open_absolute(path.parent)) as parent:
        fs._secure_directory(parent)
        with fs._held_evidence_fd(os.open(path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=parent)) as fd:
            journal._write_all(fd, data)
            os.fsync(fd)
            os.fsync(parent)
    return path


def publish_record(directory, name, raw):
    """Create-once mailbox publication, visible only after the full file fsync.

    Polling consumers may run concurrently with the producer. Original approval
    receipts retain their existing uncertain-write semantics; queue records use
    the same local evidence checks with an atomic no-replace publication.
    """
    path = record_path(directory, name)
    data = raw.encode('utf-8')
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError('approval record too large')
    temporary = '.mailbox-' + secrets.token_hex(16)
    with fs._held_evidence_fd(fs._open_absolute(path.parent)) as parent:
        fs._secure_directory(parent)
        with fs._held_evidence_fd(os.open(temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=parent)) as fd:
            journal._write_all(fd, data)
            os.fsync(fd)
        try:
            fs._rename_noreplace(parent, temporary, parent, path.name)
            os.fsync(parent)
        except FileExistsError:
            os.unlink(temporary, dir_fd=parent)
            raise
    return path


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def read_proposal(inbox, source_sha256):
    source_sha256 = identity(source_sha256)
    bundle = _strict_json_load(read_record(inbox, source_sha256 + '.proposal.json'))
    if type(bundle) is not dict or set(bundle) != {
            'manifest', 'runtime', 'published_at', 'database', 'share', 'share_root', 'new_directories'}:
        raise ValueError('invalid host proposal')
    original = manifest_from_json(bundle['manifest'])
    if manifest_to_json(original) != bundle['manifest'] or sha256(original) != source_sha256:
        raise ValueError('original canonical manifest hash differs')
    _decode_type(RuntimeEvidence, bundle['runtime'])
    published = datetime.fromisoformat(bundle['published_at'])
    if published.tzinfo is None:
        raise ValueError('dated host runtime evidence required')
    planned_directories = set(original.payload.directories)
    if (type(bundle['new_directories']) is not list
            or any(type(d) is not str or d not in planned_directories for d in bundle['new_directories'])):
        raise ValueError('new directories differ from original manifest')
    return bundle


def _original(bundle):
    return manifest_from_json(bundle['manifest'])


def _check_sqlite_input(path, *, execution_database=True):
    """Mirror E7's existing-only header/sidecar gate BEFORE opening SQLite RO.

    Even an RO SQLite connection can initialize WAL sidecars. This adapter never
    enters the worker/startup lifecycle merely to validate an approval input.
    """
    path = fs._absolute(Path(path))
    with fs._held_evidence_fd(fs._open_absolute(path.parent)) as parent:
        with fs._held_evidence_fd(os.open(path.name, fs._FILE, dir_fd=parent)) as fd:
            if execution_database:
                journal._check_file(fd, parent, path.name)
            else:
                info = os.fstat(fd)
                current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or fs._identity(info) != fs._identity(current)):
                    raise ValueError('independent regular SQLite backup required')
            header = os.pread(fd, 100, 0)
            if (len(header) != 100 or header[:16] != b'SQLite format 3\0'
                    or header[18:20] != b'\x01\x01' or int.from_bytes(header[60:64], 'big') != 9):
                raise ValueError('existing standalone schema-v9 rollback-mode SQLite required')
            for suffix in ('-wal', '-shm', '-journal'):
                try:
                    fs._absent(parent, path.name + suffix)
                except ValueError as exc:
                    raise ValueError('SQLite sidecar present; no automatic recovery') from exc


def validate_review(bundle, final, *, actor=None):
    original = _original(bundle)
    # No payload, timestamp, authority or locator from the web replaces the host original.
    if replace(final, acknowledgements=original.acknowledgements) != original:
        raise ValueError('review differs from original host manifest')
    manifest_to_json(final)
    warnings = {w.warning_id: w for w in original.payload.warnings}
    if {a.warning_id for a in final.acknowledgements} != set(warnings):
        raise ValueError('every warning requires explicit acknowledgement')
    for ack in final.acknowledgements:
        warning = warnings[ack.warning_id]
        if (ack.warning_class, ack.object_kind, ack.object_id) != (
                warning.warning_class, warning.object_kind, warning.object_id):
            raise ValueError('warning acknowledgement differs')
        if actor is not None and ack.actor != actor:
            raise ValueError('warning operator differs')


def prepare_review(bundle, *, actor, warning_ids):
    if not actor.strip():
        raise ValueError('named operator required')
    original = _original(bundle)
    expected = {w.warning_id for w in original.payload.warnings}
    if len(warning_ids) != len(set(warning_ids)) or set(warning_ids) != expected:
        raise ValueError('explicit confirmation of every exact warning required')
    timestamp = now()
    final = replace(original, acknowledgements=tuple(WarningAcknowledgement(
        w.warning_id, w.warning_class, w.object_kind, w.object_id, actor, timestamp)
        for w in original.payload.warnings))
    validate_review(bundle, final, actor=actor)
    return final


def _check_proposal(bundle, final, *, database, library_root, runtime=None):
    """Fresh RO DB/FS projection; web uses DATED host runtime, never its RO mount as RW.

    Host acceptance supplies freshly observed worker runtime instead. A web PASS
    describes the projection against published evidence, not execution admission.
    """
    original = _original(bundle)
    if not final.payload.actions:
        raise ValueError('nonempty execution manifest required by E7')
    if replace(final, acknowledgements=original.acknowledgements) != original:
        raise ValueError('manifest differs from host original')
    require_current_proposal(bundle)
    if str(fs._absolute(database)) != bundle['database'] or str(library_root) != original.payload.library_root:
        raise ValueError('database/library binding differs')
    _check_sqlite_input(database)
    evidence = runtime if runtime is not None else RuntimeEvidence(**bundle['runtime'])
    if evidence.db_backup_path:
        _check_sqlite_input(Path(evidence.db_backup_path), execution_database=False)
    context, snapshot, _plan, fingerprint, _metrics = _read_current(
        database, library_root, original.payload.windows_root)
    result = preflight_execution_manifest(final, context, snapshot, fingerprint,
        runtime=evidence)
    return result, snapshot


def check_proposal(bundle, final, *, database, library_root, runtime=None):
    return _check_proposal(bundle, final, database=database, library_root=library_root, runtime=runtime)[0]


def _require_ready(result, *, allow_unacknowledged=False):
    if result.outcome == 'READY_FOR_EXECUTION':
        return
    if (allow_unacknowledged and result.outcome == 'NOT_READY' and result.diagnostics
            and all(d.startswith('warning_unacknowledged:') for d in result.diagnostics)):
        return
    raise ValueError('preflight ' + result.outcome + ': ' + ', '.join(result.diagnostics))


def worker_runtime(library_root, *, share, share_root, runtime):
    """Existing production mount policy, with fresh observations overriding assertions."""
    root = fs._absolute(library_root)
    with fs._held_evidence_fd(fs._open_absolute(root)) as fd:
        mount = mounts._check_production_mount(root, fd, mounts._read_mounts(),
            share=share, share_root=share_root, writable=True)
        observed = observe_runtime(root)
        info = os.fstat(fd)
        binding = f'{mount.fstype}:{mount.source}:{info.st_dev}:{info.st_ino}'
        return replace(runtime, mount_identity=binding,
            mount_read_only_now=observed['mount_read_only_now'], free_bytes=observed['free_bytes'])


def publish_proposal(*, inbox, database, library_root, share, share_root, manifest, runtime):
    validate_local_output(Path(inbox) / 'proposal.json', library_root, forbidden_files=(database,))
    runtime = worker_runtime(library_root, share=share, share_root=share_root, runtime=runtime)
    bundle = {'manifest': manifest_to_json(manifest), 'runtime': asdict(runtime),
        'published_at': now(), 'database': str(fs._absolute(database)), 'share': share, 'share_root': share_root}
    result, snapshot = _check_proposal(bundle, manifest, database=database, library_root=library_root, runtime=runtime)
    _require_ready(result, allow_unacknowledged=True)
    existing = {e.relative_path for e in snapshot.entries}
    bundle['new_directories'] = [d for d in manifest.payload.directories if d not in existing]
    # Human review starts after preparation/preflight, not while it is running.
    bundle['published_at'] = now()
    source = sha256(manifest)
    write_record(inbox, source + '.proposal.json', _json(bundle))
    return source


def write_review(outbox, source_sha256, final, *, actor=None):
    actors = {a.actor for a in final.acknowledgements}
    actor = actor or (next(iter(actors)) if len(actors) == 1 else None)
    if actor is not None:
        if not actor.strip() or (actors and actors != {actor}):
            raise ValueError('review actor differs from warning acknowledgements')
        write_record(outbox, identity(source_sha256) + '.review-actor.json', _json({
            'source_sha256': source_sha256, 'approved_sha256': sha256(final), 'actor': actor}))
    return write_record(outbox, identity(source_sha256) + '.review.json', manifest_to_json(final))


def read_review_actor(outbox, source_sha256, final):
    try:
        metadata = _strict_json_load(read_record(outbox, identity(source_sha256) + '.review-actor.json'))
    except FileNotFoundError:
        # Historical acknowledgements already contain the original actor.
        actors = {a.actor for a in final.acknowledgements}
        if len(actors) == 1:
            return next(iter(actors))
        raise ValueError('review actor missing; preserve historical review and prepare a new manifest')
    if (type(metadata) is not dict or set(metadata) != {'source_sha256', 'approved_sha256', 'actor'}
            or metadata['source_sha256'] != source_sha256 or metadata['approved_sha256'] != sha256(final)
            or type(metadata['actor']) is not str or not metadata['actor'].strip()
            or any(a.actor != metadata['actor'] for a in final.acknowledgements)):
        raise ValueError('review actor binding differs')
    return metadata['actor']


def read_review(outbox, source_sha256):
    raw = read_record(outbox, identity(source_sha256) + '.review.json')
    final = manifest_from_json(raw)
    if manifest_to_json(final) != raw:
        raise ValueError('canonical review required')
    return final


def write_approval(outbox, source_sha256, final, *, actor, approved_sha256):
    if not actor.strip() or identity(approved_sha256) != sha256(final):
        raise ValueError('exact canonical SHA and named operator required')
    receipt = {'source_sha256': identity(source_sha256), 'approved_sha256': approved_sha256,
        'manifest_id': final.manifest_id, 'actor': actor, 'approved_at': now(), 'state': 'APPROVED'}
    write_record(outbox, source_sha256 + '.approval.json', _json(receipt))
    return receipt


def read_approval(outbox, source_sha256):
    return _strict_json_load(read_record(outbox, identity(source_sha256) + '.approval.json'))


def accept_approval(*, inbox, outbox, ledger, source_sha256, approved_sha256, actor,
                    database, library_root, share, share_root, runtime, runtime_lock,
                    writers_stopped=False, for_execution=False):
    """Consume once after host checks; disabled acceptance is never start authority.

    The local operator supplies the exact SHA independently of the web request.
    No command, path, runtime assertion or execution option comes from that request.
    """
    from .tools.reorganize import runtime_exclusion
    if not writers_stopped:
        raise ValueError('explicit host confirmation that affected writers are stopped required')
    inbox, outbox, ledger = (fs._absolute(Path(p)) for p in (inbox, outbox, ledger))
    for directory in (inbox, outbox, ledger):
        validate_local_output(directory / 'approval.json', library_root, forbidden_files=(database, runtime_lock))
    if any(a.is_relative_to(b) for a, b in ((ledger, inbox), (ledger, outbox), (inbox, ledger), (outbox, ledger))):
        raise ValueError('host ledger must be separate and unavailable to the web')
    bundle = read_proposal(inbox, source_sha256)
    final = read_review(outbox, source_sha256)
    validate_review(bundle, final, actor=actor)
    receipt = read_approval(outbox, source_sha256)
    if (not actor.strip() or receipt.get('actor') != actor or receipt.get('state') != 'APPROVED'
            or receipt.get('source_sha256') != source_sha256 or receipt.get('manifest_id') != final.manifest_id
            or receipt.get('approved_sha256') != identity(approved_sha256) or sha256(final) != approved_sha256
            or (share, share_root) != (bundle['share'], bundle['share_root'])):
        raise ValueError('host authorization differs from exact web approval')
    with runtime_exclusion(runtime_lock):
        if os.path.lexists(record_path(ledger, identity(final.manifest_id) + '.accepted.json')):
            raise FileExistsError('approval already spent; no execution retry or disabled-claim adoption')
        current = worker_runtime(library_root, share=share, share_root=share_root, runtime=runtime)
        _require_ready(check_proposal(bundle, final, database=database, library_root=library_root, runtime=current))
        require_current_proposal(bundle)
        if type(for_execution) is not bool:
            raise ValueError('explicit execution claim required')
        accepted = {**receipt, 'state': 'EXECUTION_CLAIMED' if for_execution else 'ACCEPTED_EXECUTION_DISABLED',
            'started': False, 'accepted_at': now()}
        # Payload identity also stops republishing the same plan with a new timestamp/envelope.
        write_record(ledger, identity(final.manifest_id) + '.accepted.json', _json(accepted))
        write_record(inbox, identity(source_sha256) + '.acceptance.json', _json(accepted))
    return accepted


def read_execution_result(inbox, source_sha256, final):
    result = _strict_json_load(read_record(inbox, identity(source_sha256) + '.result.json'))
    total = len(final.payload.actions)
    if (type(result) is not dict or result.get('source_sha256') != source_sha256
            or result.get('approved_sha256') != sha256(final) or result.get('manifest_id') != final.manifest_id
            or result.get('state') not in {'NORMAL', 'STOP'} or type(result.get('total')) is not int or result['total'] != total
            or type(result.get('completed')) is not int or not 0 <= result['completed'] <= total
            or type(result.get('remaining')) is not int or result['remaining'] != total - result['completed']
            or type(result.get('started')) is not bool or (result['completed'] and not result['started'])
            or any(type(result.get(key)) is not str or not result[key].strip() for key in
                   ('actor', 'phase', 'finished_at', 'journal_path', 'guard_path', 'cleanup_evidence_path'))
            or (result.get('error') is not None and type(result['error']) is not str)
            or not (result.get('removed_directories') is None and result.get('state') == 'STOP'
                    or type(result.get('removed_directories')) is list
                    and all(type(path) is str for path in result['removed_directories']))
            or (result['state'] == 'NORMAL' and result.get('error') is not None)
            or (result['state'] == 'NORMAL' and (result['completed'] != total or not result['started']))):
        raise ValueError('execution result binding differs')
    return result
