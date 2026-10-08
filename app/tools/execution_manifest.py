"""Generate and dry-run a V6 execution proposal. This command never executes it.

Examples:
  python -m app.tools.execution_manifest generate-manifest --db data/anime.db --library-root /mnt/anime --output /tmp/v6-manifest.json
  python -m app.tools.execution_manifest dry-run --db data/anime.db --library-root /mnt/anime --manifest /tmp/v6-manifest.json
"""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import sys
from sqlalchemy import event

from ..target_execution_manifest import (
    build_execution_manifest, manifest_from_json, manifest_to_json,
    preflight_execution_manifest, RuntimeEvidence,
)
from ..target_planner_filesystem import inventory_filesystem, plan_library, validate_inventory_root
from ..target_planner_service import load_planner_context, readonly_planner_session
from .target_plan import database_fingerprint


# Human decision 2026-10-08, limited to these current Bonus objects and owners.
# These story/extras decisions do not define Bonus or DramaCD type defaults.
_APPROVED_LOW_BONUS_CRITICALITY = (
    ((1964, 1965, 1966, 1967, 1968, 1969, 1970, 1971), 147, 115,
        'human_confirmed:Overlord story Bonus is not standalone anime'),
    ((2711, 2712, 2713), 202, 147,
        'human_confirmed:Tenki filmography/music video/video storyboard'),
)


def _approved_criticality_decisions(context, plan) -> dict:
    """Feed existing manifest authority; source names and locators are irrelevant."""
    actionable = {r.object_id for r in plan.records if r.object_kind == 'video'
        and r.status in {'READY', 'WARNING'} and r.action in {'KEEP', 'MOVE', 'QUARANTINE'}
        and r.target_relative_path}
    decisions = {}
    for video in context.videos:
        if video.id not in actionable or video.content_type.lower() != 'bonus':
            continue
        for ids, title_id, collection_id, reason in _APPROVED_LOW_BONUS_CRITICALITY:
            if video.id in ids and (video.title_id, video.collection_id) == (title_id, collection_id):
                decisions[('video', video.id)] = ('LOW', reason, 'human_operator')
    return decisions


def _mount_details(path: Path) -> tuple[str, str] | None:
    resolved = path.resolve(strict=False)
    best = None
    best_len = -1
    try:
        lines = Path('/proc/self/mountinfo').read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    for line in lines:
        before, separator, after = line.partition(' - ')
        if not separator:
            continue
        fields = before.split()
        if len(fields) < 5:
            continue
        mount = Path(fields[4].replace('\\040', ' '))
        if (resolved == mount or mount in resolved.parents) and len(str(mount)) > best_len:
            parts = after.split()
            best = (parts[0], parts[1] if len(parts) > 1 else 'UNKNOWN')
            best_len = len(str(mount))
    return best


def _mount_fstype(path: Path) -> str | None:
    details = _mount_details(path)
    return details[0] if details else None


def observe_runtime(library_root: Path) -> dict:
    """Read-only mount/platform observations; never a write capability probe."""
    root = Path(library_root)
    st = root.lstat()
    vfs = os.statvfs(root)
    mount = _mount_details(root)
    fstype, source = mount if mount else ('UNKNOWN', 'UNKNOWN')
    try:
        symbol = sys.platform.startswith('linux') and hasattr(ctypes.CDLL(None), 'renameat2')
    except OSError:
        symbol = False
    readonly = bool(vfs.f_flag & getattr(os, 'ST_RDONLY', 1))
    return {
        'mount_fstype': fstype,
        'mount_source': source,
        'mount_identity': f'{fstype}:{source}:{st.st_dev}:{st.st_ino}',
        'mount_read_only_now': readonly,
        'free_bytes': vfs.f_bavail * vfs.f_frsize,
        'safe_write_capability': 'MOUNT_NOT_PROBED',
        'rename_noreplace': 'PLATFORM_SYMBOL_ONLY_MOUNT_NOT_PROBED' if symbol else 'PLATFORM_UNAVAILABLE',
        'scanner_maintenance': 'NOT_PROVEN',
        'snapshot_capability': 'NOT_PROVEN',
    }


def validate_local_output(output: Path, *library_roots: Path, forbidden_files: tuple[Path, ...] = ()) -> Path:
    """Reject direct and symlinked aliases of the media root before any write."""
    output = Path(output).absolute()
    resolved = output.resolve(strict=False)
    repo = Path(__file__).resolve().parents[2]
    if resolved == repo or repo in resolved.parents:
        raise ValueError('manifest output must be outside the repository')
    for library_root in library_roots:
        library = Path(library_root).resolve(strict=False)
        if resolved == library or library in resolved.parents:
            raise ValueError('manifest output must be outside the library root')
    if output.is_symlink():
        raise ValueError('manifest output cannot be a symlink')
    if output.is_dir():
        raise ValueError('manifest output must be a file path')
    for forbidden in forbidden_files:
        forbidden = Path(forbidden).absolute()
        if resolved == forbidden.resolve(strict=False) or (output.exists() and forbidden.exists() and output.samefile(forbidden)):
            raise ValueError('manifest output cannot replace an input file')
    if not output.parent.is_dir():
        raise ValueError('manifest output parent must already exist')
    fstype = _mount_fstype(output.parent)
    if fstype is None or fstype in {'cifs', 'smb3', 'nfs', 'nfs4', '9p'} or fstype.startswith(('fuse.sshfs', 'fuse.rclone')):
        raise ValueError('manifest output requires a verified local filesystem')
    return output


def _read_current(database_path: Path, library_root: Path, windows_root: str | None):
    before = database_fingerprint(database_path)
    with readonly_planner_session(database_path) as session:
        statements = []
        def select_only(_conn, _cursor, statement, _parameters, _context, _executemany):
            if not statement.lstrip().upper().startswith('SELECT'):
                raise RuntimeError('manifest preload attempted a non-SELECT statement')
            statements.append(statement)
        event.listen(session.get_bind(), 'before_cursor_execute', select_only)
        context = load_planner_context(session)
        session_state = [len(session.new), len(session.dirty), len(session.deleted)]
    snapshot = inventory_filesystem(library_root)
    plan = plan_library(context, snapshot, windows_root=windows_root)
    after = database_fingerprint(database_path)
    if before != after:
        raise RuntimeError('database changed during read-only manifest planning')
    return context, snapshot, plan, before, {
        'sql_select_count': len(statements), 'session_state': session_state,
        'filesystem_traversals': 1, 'db_unchanged': before == after,
        'nas_write_operations': 0,
    }


def _atomic_local_write(path: Path, content: str):
    descriptor, temporary = tempfile.mkstemp(prefix='.v6-manifest-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('generate-manifest', 'dry-run'):
        p = sub.add_parser(command)
        p.add_argument('--db', type=Path, required=True)
        p.add_argument('--library-root', type=Path, required=True)
        p.add_argument('--windows-root', help='Actual mapped or UNC absolute Windows root')
        if command == 'generate-manifest':
            p.add_argument('--output', type=Path, required=True)
        else:
            p.add_argument('--manifest', type=Path, required=True)
            p.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    validate_inventory_root(args.library_root)
    if args.command == 'generate-manifest':
        path = validate_local_output(args.output, args.library_root, forbidden_files=(args.db,))
    else:
        manifest = manifest_from_json(args.manifest.read_text(encoding='utf-8'))
        path = validate_local_output(args.output, args.library_root, Path(manifest.payload.library_root),
            forbidden_files=(args.db, args.manifest))
    context, snapshot, plan, db, metrics = _read_current(args.db, args.library_root, args.windows_root)
    if args.command == 'generate-manifest':
        observations = observe_runtime(args.library_root)
        mount_identity = observations['mount_identity']
        manifest = build_execution_manifest(context, snapshot, plan, db,
            mount_identity=mount_identity, windows_root=args.windows_root,
            criticality_decisions=_approved_criticality_decisions(context, plan))
        _atomic_local_write(path, manifest_to_json(manifest))
        print(json.dumps({'manifest_id': manifest.manifest_id, 'output': str(path), **metrics,
            'actions': len(manifest.payload.actions), 'counts': manifest.payload.counts,
            'graph': asdict(manifest.payload.graph), 'runtime_observations': observations,
            'generation_diagnostics': manifest.payload.generation_diagnostics},
            ensure_ascii=True))
        return 2 if manifest.payload.generation_diagnostics else 0
    observations = observe_runtime(args.library_root)
    result = preflight_execution_manifest(manifest, context, snapshot, db,
        runtime=RuntimeEvidence(actual_windows_root=args.windows_root,
            mount_identity=observations['mount_identity'],
            mount_read_only_now=observations['mount_read_only_now'],
            free_bytes=observations['free_bytes']), windows_root=args.windows_root)
    report = asdict(result)
    report.update(graph=asdict(manifest.payload.graph), counts=manifest.payload.counts,
        runtime_observations=observations, **metrics)
    _atomic_local_write(path, json.dumps(report, ensure_ascii=True, indent=2))
    print(json.dumps({'outcome': result.outcome, 'manifest_id': result.manifest_id, 'output': str(path)}, ensure_ascii=True))
    return 0 if result.outcome == 'READY_FOR_EXECUTION' else 2 if result.outcome in {'NOT_READY', 'STALE'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
