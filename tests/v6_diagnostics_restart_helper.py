"""E5B extends E5A's real crash helper; all inputs are private /tmp artifacts."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import sys

from app import target_execution_coordination as coordination
from app import target_execution_database as database
from app import target_execution_filesystem as fs
from app import target_execution_journal as journal
from app.target_execution_manifest import ExecutionManifest, _decode_type, _strict_json_load, manifest_to_json
from app.target_execution_diagnostics import inspect_scratch_video_move
from v6_database_case import prepare_database
from v6_database_restart_helper import create as e5a_create


def paths_for(parent):
    manifest_path = parent / 'manifest.json'
    manifest = _decode_type(ExecutionManifest, _strict_json_load(manifest_path.read_text(encoding='utf-8')))
    root = Path(manifest.payload.library_root)
    return manifest, dict(root=str(root), source=str(root / manifest.payload.actions[0].source),
        target=str(root / manifest.payload.actions[0].target), database=str(root.parent / (root.name + '.sqlite')),
        journal=str(root.parent / (root.name + '.journal.jsonl')),
        evidence=str(root.parent / (root.name + '.identity.json')), manifest=str(manifest_path))


def create(parent, boundary):
    if boundary in {'after_fs_verified', 'after_commit', 'after_db_reconciled'}:
        return e5a_create(parent, boundary)
    append, rename, apply = journal.DurableJournal.append, fs._rename_noreplace, database._ScratchVideoDatabase._apply
    writers = []

    def reached(db=None):
        manifest, paths = paths_for(parent)
        phase = {'before_rename': 'PENDING', 'after_rename': 'PENDING',
            'open_transaction': 'FS_VERIFIED', 'after_complete': 'COMPLETE'}[boundary]
        assert writers[0].inspect_live(manifest)[-1].state == phase
        assert Path(paths['source']).exists() is (boundary == 'before_rename')
        assert Path(paths['target']).exists() is (boundary != 'before_rename')
        if db is not None:
            assert db._connection.in_transaction
            assert db._connection.execute('SELECT relative_path FROM videos WHERE id=100').fetchone() == (manifest.payload.actions[0].target,)
            artifact = Path(paths['database'] + '-journal')
            assert artifact.is_file() and artifact.stat().st_size > 512
        print(json.dumps(dict(paths=paths, pid=os.getpid(), boundary=boundary,
            boundary_assertions_completed=True)), flush=True)
        while True:
            signal.pause()

    def appended(writer, entry):
        writers[:] = [writer]
        append(writer, entry)
        if boundary == 'after_complete' and entry.state == 'COMPLETE':
            reached()

    def renamed(*args):
        if boundary == 'before_rename':
            reached()
        rename(*args)
        if boundary == 'after_rename':
            reached()

    def applied(db):
        if boundary == 'open_transaction':
            db._connection.execute('PRAGMA cache_size=1')
        apply(db)
        if boundary == 'open_transaction':
            reached(db)

    journal.DurableJournal.append, fs._rename_noreplace = appended, renamed
    database._ScratchVideoDatabase._apply = applied
    return e5a_create(parent, boundary)


def completed(parent):
    with coordination.ScratchActionSession.create(parent) as session:
        manifest, _, _, _ = prepare_database(session)
        (parent / 'manifest.json').write_text(manifest_to_json(manifest), encoding='utf-8')
        assert session.execute_video_move(manifest, manifest.payload.actions[0], action_index=0).outcome == 'COMPLETE'
    _, paths = paths_for(parent)
    print(json.dumps(dict(paths=paths, pid=os.getpid(), boundary='completed', boundary_assertions_completed=True)), flush=True)


def inspect(paths, boundary, variation):
    manifest = _decode_type(ExecutionManifest, _strict_json_load(Path(paths['manifest']).read_text(encoding='utf-8')))
    connections = []
    def read_only(event, args):
        if event == 'open':
            assert not args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND), (event, args)
        if event == 'sqlite3.connect':
            assert str(args[0]).endswith('?mode=ro'), args
            connections.append(str(args[0]))
        assert event not in {'os.mkdir', 'os.rmdir', 'os.rename', 'os.remove', 'os.link',
            'os.symlink', 'os.truncate', 'os.chmod', 'os.chown', 'os.utime'}, (event, args)
        if event in {'open', 'os.listdir', 'os.scandir'} and isinstance(args[0], (str, bytes)):
            assert not os.fsdecode(args[0]).startswith('/mnt/'), (event, args)
    sys.addaudithook(read_only)
    def no_execution(*args, **kwargs):
        raise AssertionError('E5B minted execution authority')
    for cls in (coordination.ScratchActionSession, fs.ScratchRenameSession, journal.DurableJournal):
        cls.create = no_execution
    report = inspect_scratch_video_move(manifest, manifest.payload.actions[0], action_index=0,
        scratch_root=Path(paths['root']), journal_path=Path(paths['journal']), evidence_path=Path(paths['evidence']))
    if variation == 'early_exit':
        os._exit(0)
    assert report.outcome == 'STOP' and report.manual_review_required is True
    assert report.identity_continuity == report.filesystem.target.physical_identity_match == 'UNKNOWN'
    assert report.database.full_post_state == report.database.transaction_state == 'UNKNOWN'
    expected = {
        'before_rename': ('PENDING', 'SOURCE_ONLY', 'OLD_VALUES'),
        'after_rename': ('PENDING', 'TARGET_ONLY', 'OLD_VALUES'),
        'after_fs_verified': ('FS_VERIFIED', 'TARGET_ONLY', 'OLD_VALUES'),
        'after_commit': ('FS_VERIFIED', 'TARGET_ONLY', 'NEW_VALUES'),
        'after_db_reconciled': ('DB_RECONCILED', 'TARGET_ONLY', 'NEW_VALUES'),
        'after_complete': ('COMPLETE', 'TARGET_ONLY', 'NEW_VALUES'),
        'completed': ('COMPLETE', 'TARGET_ONLY', 'NEW_VALUES'),
        'open_transaction': ('FS_VERIFIED', 'TARGET_ONLY', 'UNINSPECTED'),
    }[boundary]
    assert (report.filesystem.journal.last_action_phase, report.filesystem.observed_state,
        report.database.locator_state) == expected
    assert report.filesystem.journal.status == report.filesystem.evidence.status == 'VALID'
    assert len(connections) == (0 if boundary == 'open_transaction' else 1)
    if boundary == 'open_transaction':
        assert report.database.status == 'DB_UNINSPECTED' and 'SQLITE_SIDECAR' in report.stop_reasons
    print(json.dumps(dict(pid=os.getpid(), assertions_completed=True, report=asdict(report),
        sqlite_connections=len(connections))), flush=True)


if __name__ == '__main__':
    if sys.argv[1] == 'create':
        create(Path(sys.argv[2]), sys.argv[3])
    elif sys.argv[1] == 'completed':
        completed(Path(sys.argv[2]))
    else:
        inspect(json.loads(sys.argv[2]), sys.argv[3], sys.argv[4])
