"""Administrative existing-library entrypoint for E7; never a web/startup service.

The host administrator supplies the isolated RW mount and stops other writers.
This command cannot mount shares, read SMB credentials, create SQLite or deploy.
"""
from contextlib import contextmanager
from dataclasses import asdict
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sys

from app import target_execution_filesystem as fs
from app import target_execution_journal as journal
from app.database import make_engine
from app.maintenance import MaintenanceBusy, MaintenanceStopped, ScratchMaintenance
from app.target_execution_cleanup import approve_cleanup
from app.target_execution_guard import complete_reorganization
from app.target_execution_manifest import RuntimeEvidence, manifest_from_json, manifest_to_json
from app.target_execution_sequence import ScratchManifestExecution
from app.tools.execution_manifest import _read_runtime_evidence, validate_local_output
from app.target_planner_filesystem import validate_inventory_root


@contextmanager
def runtime_exclusion(path: Path, *, stop_execution=None):
    """Hold the deployment's existing runtime flock; a missing lock is STOP."""
    path = fs._absolute(Path(path))
    validate_local_output(path)
    with fs._held_evidence_fd(fs._open_absolute(path.parent)) as parent:
        fs._secure_directory(parent)
        fd = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        with fs._held_evidence_fd(fd):
            journal._check_file(fd, parent, path.name)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MaintenanceBusy('STOP: application or another worker owns runtime.lock') from exc
            try:
                yield
                journal._check_file(fd, parent, path.name)
            except BaseException as exc:
                if stop_execution is not None:
                    stop_execution(exc)
                raise


def execute_approved_reorganization(*, database: Path, library_root: Path, share: str,
                                    share_root: str, manifest, approved_sha256: str,
                                    runtime: RuntimeEvidence, runtime_lock: Path, cleanup_decision,
                                    verify_content=(), progress=None, admission_check=None):
    """Compose existing locks, E7, E7H and completion. No retry/adoption after STOP."""
    canonical = manifest_to_json(manifest)
    if hashlib.sha256(canonical.encode()).hexdigest() != approved_sha256:
        raise MaintenanceStopped('STOP: canonical manifest approval differs')
    if manifest.payload.library_root != str(library_root):
        raise MaintenanceStopped('STOP: approved library root differs')
    started = False
    revoked = False
    operation_owner = None
    total = len(manifest.payload.actions)

    def phase(name, completed=0):
        if progress is not None:
            progress({'phase': name, 'completed': completed, 'total': total, 'remaining': total - completed})

    def decide_cleanup(proposal):
        phase('WAITING_FOR_CLEANUP_APPROVAL', total)
        decision = cleanup_decision(proposal)
        phase('E7H_CLEANUP', total)
        return decision

    def stop_execution(exc):
        nonlocal revoked
        guard = getattr(operation_owner, '_persistent_guard', None)
        if started and not revoked and guard is not None:
            revoked = True
            from app.target_execution_guard import _revoke_transition
            _revoke_transition(guard.path, guard._identity, exc)

    try:
        phase('RUNTIME_EXCLUSION')
        with runtime_exclusion(runtime_lock, stop_execution=stop_execution):
            if admission_check is not None:
                admission_check()
            with ScratchManifestExecution.open_existing(library_root, database_path=database,
                    share=share, share_root=share_root) as owner:
                operation_owner = owner
                gate = ScratchMaintenance.existing(database, library_root)
                with gate.maintenance() as lease:
                    started = True
                    phase('E7_PREFLIGHT')
                    owner.execute(manifest, maintenance_owner=lease,
                        cleanup_decision=decide_cleanup if cleanup_decision is not None else None,
                        resume_scanning=True, runtime_evidence=runtime,
                        approved_manifest_sha256=approved_sha256, verify_content=verify_content, progress=progress)
                phase('NORMAL_VERIFICATION', total)
                engine = make_engine('sqlite:///' + database.as_uri() + '?mode=rw&uri=true', maintenance=gate)
                try:
                    complete_reorganization(engine, library_root)
                finally:
                    engine.dispose()
                result = {'manifest_id': manifest.manifest_id, 'state': 'NORMAL',
                    'removed_directories': list(owner.cleanup_outcome.removed)}
        phase('NORMAL', total)
        return result
    except BaseException as exc:
        # In particular, a failed wrapper finalization must not leave a newly
        # published NORMAL receipt looking successful to the next app startup.
        stop_execution(exc)
        raise


def _read_manifest(path, root, database):
    validate_local_output(path, root, forbidden_files=(database,))
    validate_inventory_root(path)
    with fs._held_evidence_fd(fs._open_absolute(path.parent)) as parent:
        with fs._held_evidence_fd(os.open(path.name, fs._FILE, dir_fd=parent)) as fd:
            before = journal._check_file(fd, parent, path.name)
            if before.st_size > 16 * 1024 * 1024:
                raise ValueError('manifest exceeds supported guard size')
            raw = os.pread(fd, before.st_size + 1, 0).decode('utf-8')
            manifest = manifest_from_json(raw)
            canonical = manifest_to_json(manifest)
            if raw not in (canonical, canonical + '\n'):
                raise ValueError('canonical manifest file required')
            after = journal._check_file(fd, parent, path.name)
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('manifest changed during approval read')
            return manifest


def _interactive_cleanup(database, actor):
    def decide(proposal):
        path = database.with_suffix('.cleanup-proposal.json')
        raw = (json.dumps(asdict(proposal), ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode()
        with fs._held_evidence_fd(fs._open_absolute(path.parent)) as parent:
            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=parent)
            with fs._held_evidence_fd(fd):
                journal._write_all(fd, raw)
                os.fsync(fd)
                os.fsync(parent)
        print(json.dumps({'cleanup_proposal': str(path), 'proposal_sha256': proposal.proposal_sha256,
            'candidates': [p.path for p in proposal.candidates]}), flush=True)
        selected = json.loads(input('Approve exact empty-directory paths as a JSON array ([] preserves all): '))
        if type(selected) is not list or any(type(p) is not str for p in selected):
            raise ValueError('explicit directory path array required')
        if input('Confirm the cleanup proposal SHA-256: ').strip() != proposal.proposal_sha256:
            raise ValueError('cleanup proposal approval differs')
        return (approve_cleanup(proposal, tuple(selected), actor=actor,
            reason='administrator approved exact live cleanup proposal') if selected else None)
    return decide


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--library-root', type=Path, required=True)
    parser.add_argument('--share', required=True, help='Exact //server/share of the authorized RW media mount')
    parser.add_argument('--share-root', default='/')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--approve-manifest-sha256', required=True)
    parser.add_argument('--runtime-evidence', type=Path, required=True)
    parser.add_argument('--runtime-lock', type=Path, required=True, help='Existing deployment runtime.lock')
    parser.add_argument('--windows-root', required=True)
    parser.add_argument('--actor', required=True)
    parser.add_argument('--verify-content', action='append', default=[], metavar='SOURCE_RELATIVE_PATH',
        help='Optional targeted FULL verification; repeat only for explicitly selected manifest sources')
    args = parser.parse_args(argv)
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        if not args.actor.strip():
            raise ValueError('administrator actor required')
        database, root = fs._absolute(args.db), fs._absolute(args.library_root)
        manifest = _read_manifest(fs._absolute(args.manifest), root, database)
        external = _read_runtime_evidence(args.runtime_evidence, root)
        runtime = RuntimeEvidence(**external, actual_windows_root=args.windows_root)
        result = execute_approved_reorganization(database=database, library_root=root, share=args.share,
            share_root=args.share_root, manifest=manifest, approved_sha256=args.approve_manifest_sha256,
            runtime=runtime, runtime_lock=args.runtime_lock, cleanup_decision=_interactive_cleanup(database, args.actor),
            verify_content=tuple(args.verify_content))
        print(json.dumps(result), flush=True)
        return 0
    except Exception as exc:
        print(f'STOP: {exc}. Preserve all files/evidence; do not retry or restore automatically.', file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
