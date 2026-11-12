"""Fixed isolated worker for the host coordinator. No Docker, mount or shell API.

All paths/capabilities come from host-private configuration. Browser requests
carry only exact identities and explicit decisions; they cannot choose options.
"""
from dataclasses import dataclass, fields, replace
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import sys

from app import reorganization_approval as approval
from app import target_execution_filesystem as fs
from app import target_execution_journal as journal
from app.reorganization_cleanup_web import WebCleanupDecision
from app.reorganization_execution import execute_from_approval
from app.reorganization_service import ServiceProgress
from app.target_execution_manifest import (
    RuntimeEvidence, build_execution_manifest, manifest_from_json, manifest_to_json, _strict_json_load, _decode_type,
)
from app.tools.execution_manifest import (
    _read_current, _approved_criticality_decisions, validate_local_output,
)
from app.tools.target_plan import database_fingerprint
from app.tools.reorganize import runtime_exclusion


@dataclass(frozen=True)
class WorkerConfig:
    database: Path
    library_root: Path
    share: str
    share_root: str
    windows_root: str
    inbox: Path
    outbox: Path
    ledger: Path
    runtime_lock: Path
    backups: Path
    service: Path
    mount_read_write_checked: bool = False
    safe_write_capability_proven: bool = False
    rename_noreplace_capability_proven: bool = False

    @classmethod
    def load(cls, path):
        path = fs._absolute(Path(path))
        # Config belongs to the administrator; it may be root-owned inside the
        # non-root worker, unlike execution evidence owned by the worker UID.
        with fs._held_evidence_fd(fs._open_absolute(path.parent)) as parent:
            with fs._held_evidence_fd(os.open(path.name, fs._FILE, dir_fd=parent)) as fd:
                import stat
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 65536:
                    raise ValueError('private regular host worker configuration required')
                data = _strict_json_load(os.pread(fd, info.st_size + 1, 0).decode())
        names = {f.name for f in fields(cls)}
        if type(data) is not dict or set(data) != names:
            raise ValueError('exact worker configuration fields required')
        paths = {'database', 'library_root', 'inbox', 'outbox', 'ledger', 'runtime_lock', 'backups', 'service'}
        for key in names:
            if key in paths:
                data[key] = fs._absolute(Path(data[key]))
            elif key.endswith('_proven') or key == 'mount_read_write_checked':
                if type(data[key]) is not bool: raise ValueError('boolean host capability required')
            elif type(data[key]) is not str or not data[key]:
                raise ValueError('nonempty host binding required')
        return cls(**data)


class WorkerStopped(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.error_code = code


def error_code(error):
    if isinstance(error, WorkerStopped):
        return error.error_code
    detail = str(error).lower()
    for fragments, code in (
        (('proposal expired',), 'MANIFEST_EXPIRED'),
        (('request expired',), 'REQUEST_EXPIRED'),
        (('snapshot',), 'SNAPSHOT_FAILED'),
        (('database changed', 'db_fingerprint', 'database_fingerprint', 'db_changed'), 'DATABASE_CHANGED'),
        (('review/blocked', 'plan_record_not_actionable', 'generation_diagnostics'), 'PLAN_UNRESOLVED'),
        (('capabilit',), 'CAPABILITIES_UNVERIFIED'),
        (('mount',), 'RW_MOUNT_UNAVAILABLE'),
        (('approval', 'review actor', 'start request differs'), 'APPROVAL_INVALID'),
    ):
        if any(fragment in detail for fragment in fragments):
            return code
    return 'WORKER_FAILED'


def read_request(config, request_id, kind, *, check_age=True):
    request_id = approval.identity(request_id)
    value = _strict_json_load(approval.read_record(config.outbox, request_id + '.' + kind + '-request.json'))
    required = ({'preview_db_sha256'} if kind == 'prepare' else
                {'source_sha256', 'approved_sha256', 'manifest_id'})
    if (type(value) is not dict or set(value) != {'version', 'kind', 'request_id', 'actor', 'requested_at'} | required
            or type(value['version']) is not int or value['version'] != 1 or value['kind'] != kind
            or value['request_id'] != request_id or type(value['actor']) is not str
            or not value['actor'].strip() or len(value['actor']) > 128
            or any(ord(c) < 32 for c in value['actor'])):
        raise ValueError('invalid exact web request')
    timestamp = datetime.fromisoformat(value['requested_at'])
    if timestamp.tzinfo is None or (check_age and not 0 <= (datetime.now(timezone.utc) - timestamp).total_seconds() <= approval.MAX_AGE_SECONDS):
        raise WorkerStopped('REQUEST_EXPIRED', 'web request expired; no automatic retry')
    for key in required:
        approval.identity(value[key])
    if kind == 'start' and value['source_sha256'] != request_id:
        raise ValueError('start source differs')
    return value


def _runtime(config, request_id, stage):
    """Current coordinator observations, using the existing RuntimeEvidence type.

    Configuration flags only enable the workflow. The host validates the actual
    mount-bound capability evidence and writer acknowledgements before publishing
    each private stage receipt; no browser can write this directory.
    """
    if not all(type(getattr(config, name)) is bool and getattr(config, name) for name in
               ('mount_read_write_checked', 'safe_write_capability_proven', 'rename_noreplace_capability_proven')):
        raise WorkerStopped('CAPABILITIES_UNVERIFIED', 'RW mount capability policy is disabled')
    try:
        runtime = _decode_type(RuntimeEvidence, _strict_json_load(approval.read_record(
            config.ledger, approval.identity(request_id) + '.' + stage + '-runtime.json')))
    except (OSError, ValueError, TypeError) as exc:
        raise WorkerStopped('CAPABILITIES_UNVERIFIED', 'current coordinator runtime evidence is unavailable') from exc
    if not all((runtime.mount_read_write_checked, runtime.safe_write_capability_proven,
                runtime.rename_noreplace_capability_proven)):
        raise WorkerStopped('CAPABILITIES_UNVERIFIED', 'actual RW mount capabilities are not proven')
    if not runtime.scanner_paused_through_verification or not runtime.inventory_writer_paused_through_verification:
        raise WorkerStopped('WRITERS_NOT_STOPPED', 'current writer exclusion is not proven')
    return runtime


def _observe(config, runtime):
    try:
        return approval.worker_runtime(config.library_root, share=config.share,
            share_root=config.share_root, runtime=runtime)
    except (OSError, ValueError) as exc:
        raise WorkerStopped('RW_MOUNT_UNAVAILABLE', 'authorized RW mount is unavailable') from exc


def _backup(config, request_id, manifest):
    destination = validate_local_output(config.backups / (request_id + '.sqlite'), config.library_root,
        forbidden_files=(config.database, config.runtime_lock))
    approval._check_sqlite_input(config.database)
    # Exact independent bytes, no SQLite backup startup/recovery. The host has
    # stopped writers; both the source descriptor and full fingerprint are checked.
    with fs._held_evidence_fd(fs._open_absolute(config.database.parent)) as parent:
        with fs._held_evidence_fd(os.open(config.database.name, fs._FILE, dir_fd=parent)) as source:
            before = journal._check_file(source, parent, config.database.name)
            with fs._held_evidence_fd(fs._open_absolute(destination.parent)) as target_parent:
                fs._secure_directory(target_parent)
                with fs._held_evidence_fd(os.open(destination.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600, dir_fd=target_parent)) as target:
                    offset = 0
                    while offset < before.st_size:
                        raw = os.pread(source, min(1024 * 1024, before.st_size - offset), offset)
                        if not raw: raise ValueError('SQLite changed during backup')
                        journal._write_all(target, raw)
                        offset += len(raw)
                    os.fsync(target)
                os.fsync(target_parent)
            after = journal._check_file(source, parent, config.database.name)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('SQLite changed during backup')
    actual = database_fingerprint(destination)
    expected = manifest.payload.db_fingerprint
    if any(actual[key] != getattr(expected, key) for key in ('sha256', 'size', 'user_version')):
        raise ValueError('backup differs from planned SQLite')
    return destination


def plan(config, request_id):
    with runtime_exclusion(config.runtime_lock):
        return _plan(config, request_id)


def _plan(config, request_id):
    request = read_request(config, request_id, 'prepare')
    runtime = _runtime(config, request_id, 'plan')
    approval._check_sqlite_input(config.database)
    observed = _observe(config, runtime)
    context, snapshot, target, fingerprint, _ = _read_current(config.database, config.library_root, config.windows_root)
    if fingerprint['sha256'] != request['preview_db_sha256']:
        raise WorkerStopped('DATABASE_CHANGED', 'database changed since preview; prepare a new preview')
    manifest = build_execution_manifest(context, snapshot, target, fingerprint,
        mount_identity=observed.mount_identity, windows_root=config.windows_root,
        criticality_decisions=_approved_criticality_decisions(context, target))
    if not manifest.payload.actions or manifest.payload.generation_diagnostics or target.collisions:
        raise WorkerStopped('PLAN_UNRESOLVED', 'REVIEW/BLOCKED or empty manifest: ' + ', '.join(manifest.payload.generation_diagnostics))
    _backup(config, request_id, manifest)
    approval.write_record(config.ledger, request_id + '.planned.json', manifest_to_json(manifest))
    return dict(manifest_id=manifest.manifest_id, created_at=manifest.created_at, source_sha256=approval.sha256(manifest))


def publish(config, request_id, snapshot):
    with runtime_exclusion(config.runtime_lock):
        return _publish(config, request_id, snapshot)


def _publish(config, request_id, snapshot):
    read_request(config, request_id, 'prepare')
    runtime = _runtime(config, request_id, 'publish')
    manifest = manifest_from_json(approval.read_record(config.ledger, request_id + '.planned.json'))
    if (type(snapshot) is not dict or set(snapshot) != {'snapshot_id', 'snapshot_at', 'snapshot_manifest_id'}
            or any(type(v) is not str or not v.strip() for v in snapshot.values())
            or snapshot['snapshot_manifest_id'] != manifest.manifest_id):
        raise WorkerStopped('SNAPSHOT_FAILED', 'exact independently created snapshot receipt required')
    p = manifest.payload.db_fingerprint
    runtime = replace(runtime, snapshot_capable=bool(snapshot['snapshot_id']), **snapshot,
        db_backup_path=str(config.backups / (request_id + '.sqlite')), db_backup_sha256=p.sha256,
        db_backup_size=p.size, db_backup_user_version=p.user_version, db_backup_manifest_id=manifest.manifest_id,
        actual_windows_root=config.windows_root)
    source = approval.publish_proposal(inbox=config.inbox, database=config.database, library_root=config.library_root,
        share=config.share, share_root=config.share_root, manifest=manifest, runtime=runtime)
    return {'source_sha256': source}


def _start_inputs(config, request_id, *, check_age=True):
    request = read_request(config, request_id, 'start', check_age=check_age)
    bundle = approval.read_proposal(config.inbox, request_id)
    final = approval.read_review(config.outbox, request_id)
    if (approval.sha256(final) != request['approved_sha256'] or final.manifest_id != request['manifest_id']
            or request['actor'] != approval.read_review_actor(config.outbox, request_id, final)):
        raise WorkerStopped('APPROVAL_INVALID', 'start request differs from exact reviewed approval')
    approval.validate_review(bundle, final, actor=request['actor'])
    receipt = approval.read_approval(config.outbox, request_id)
    if (receipt.get('state') != 'APPROVED' or receipt.get('actor') != request['actor']
            or any(receipt.get(key) != request[key] for key in ('source_sha256', 'approved_sha256', 'manifest_id'))):
        raise WorkerStopped('APPROVAL_INVALID', 'exact approval receipt differs from requested start')
    if (bundle['runtime']['actual_windows_root'] != config.windows_root or final.payload.library_root != str(config.library_root)
            or (bundle['share'], bundle['share_root']) != (config.share, config.share_root)):
        raise WorkerStopped('APPROVAL_INVALID', 'worker environment differs from published proposal')
    return request, bundle, final


def _identity_result(request, state):
    return dict(state=state, **{key: request[key] for key in ('source_sha256', 'approved_sha256', 'manifest_id')})


def check_idle(config, request_id):
    """Positive read-only proof for a guarded app return; never retry authority."""
    request = read_request(config, request_id, 'start', check_age=False)
    for directory, name in ((config.ledger, request['manifest_id'] + '.accepted.json'),
            (config.inbox, request_id + '.acceptance.json'), (config.inbox, request_id + '.result.json')):
        if os.path.lexists(directory / name):
            raise WorkerStopped('EVIDENCE_UNCERTAIN', 'execution claim or result exists; no automatic restart')
    from app.target_execution_guard import _require_no_evidence
    try:
        approval._check_sqlite_input(config.database)
        _require_no_evidence(config.database)
    except Exception as exc:
        raise WorkerStopped('EVIDENCE_UNCERTAIN', 'SQLite or execution evidence does not prove an idle state') from exc
    return _identity_result(request, 'E7_NOT_STARTED')


def check_start(config, request_id):
    """Online precheck without runtime lock or writes; E7 repeats fresh checks."""
    request, bundle, final = _start_inputs(config, request_id)
    approval.require_current_proposal(bundle)
    check_idle(config, request_id)
    current = database_fingerprint(config.database)
    expected = final.payload.db_fingerprint
    if any(current[key] != getattr(expected, key) for key in ('sha256', 'size', 'user_version', 'mtime_ns')):
        raise WorkerStopped('DATABASE_CHANGED', 'database changed since approved manifest')
    approval._require_ready(approval.check_proposal(bundle, final, database=config.database,
        library_root=config.library_root))
    approval.require_current_proposal(bundle)
    return _identity_result(request, 'READY')


def execute(config, request_id):
    request, bundle, final = _start_inputs(config, request_id)
    approval.require_current_proposal(bundle)
    fresh = _runtime(config, request_id, 'execute')
    # Snapshot/backup remain bound to the reviewed manifest. Current capability
    # and writer observations replace dated runtime assertions for admission.
    runtime = replace(RuntimeEvidence(**bundle['runtime']),
        scanner_paused_through_verification=fresh.scanner_paused_through_verification,
        inventory_writer_paused_through_verification=fresh.inventory_writer_paused_through_verification,
        mount_read_write_checked=fresh.mount_read_write_checked,
        safe_write_capability_proven=fresh.safe_write_capability_proven,
        rename_noreplace_capability_proven=fresh.rename_noreplace_capability_proven)
    runtime = _observe(config, runtime)
    if (config.ledger / (final.manifest_id + '.accepted.json')).exists():
        raise FileExistsError('approval already spent; preserve previous run and service status')
    cleanup = WebCleanupDecision(database=config.database, ledger=config.ledger,
        source_sha256=request_id, approved_sha256=request['approved_sha256'])
    with ServiceProgress(config.service, library_root=config.library_root) as progress:
        progress.workflow('execution', 'Ověřování přesného schválení a runtime podmínek.',
            source_sha256=request_id, result_url='/reorganization/approval/' + request_id)
        result = execute_from_approval(inbox=config.inbox, outbox=config.outbox, ledger=config.ledger,
            source_sha256=request_id, approved_sha256=request['approved_sha256'], actor=request['actor'],
            database=config.database, library_root=config.library_root, share=config.share, share_root=config.share_root,
            runtime=runtime, runtime_lock=config.runtime_lock,
            writers_stopped=runtime.scanner_paused_through_verification and runtime.inventory_writer_paused_through_verification,
            cleanup_decision=cleanup, progress=progress.observe)
        progress.finish('NORMAL')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('command', choices=('plan', 'publish', 'execute', 'check-start', 'check-idle'))
    parser.add_argument('--request-id', required=True)
    args = parser.parse_args(argv)
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        config = WorkerConfig.load(args.config)
        if args.command == 'publish':
            raw = sys.stdin.buffer.read(65537)
            if len(raw) > 65536: raise ValueError('snapshot receipt too large')
            result = publish(config, args.request_id, _strict_json_load(raw.decode()))
        else:
            result = {'plan': plan, 'execute': execute, 'check-start': check_start, 'check-idle': check_idle}[args.command](config, args.request_id)
        if args.command == 'execute':
            # The complete cleanup/report already lives in the original inbox.
            # Keep the host control response bounded even for long directory lists.
            result = {key: result[key] for key in ('state', 'source_sha256', 'approved_sha256', 'manifest_id')}
        print(approval._json(result), flush=True)
        return 0
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        print('STOP_JSON:' + approval._json({'version': 1, 'error_code': error_code(exc),
            'detail': f'{type(exc).__name__}: {exc}'[-8192:]}), file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
