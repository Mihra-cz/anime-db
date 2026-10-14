"""E5A's three concrete process-death boundaries; isolated scratch data only."""
from contextlib import closing
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
from urllib.parse import quote

from app import target_execution_database as database
from app import target_execution_journal as journal
from app.target_execution_coordination import ScratchActionSession
from app.target_execution_manifest import manifest_from_json, manifest_to_json
from app.target_execution_reconciliation import inspect_scratch_action
from app.target_planner_service import load_planner_context, readonly_planner_session
from v6_database_case import prepare_database


def create(parent, boundary):
    with ScratchActionSession.create(parent) as session:
        manifest, source, target, path = prepare_database(session)
        manifest_path = parent / 'manifest.json'
        manifest_path.write_text(manifest_to_json(manifest), encoding='utf-8')
        paths = dict(root=str(session.root), source=str(source), target=str(target),
            database=str(path), journal=str(session.journal_path), evidence=str(session.evidence_path),
            manifest=str(manifest_path))
        append, apply, commit = journal.DurableJournal.append, database._ScratchVideoDatabase._apply, database._ScratchVideoDatabase._commit
        writer_seen = []

        def reached():
            phase = 'DB_RECONCILED' if boundary == 'after_db_reconciled' else 'FS_VERIFIED'
            assert writer_seen[0].inspect_live(manifest)[-1].state == phase
            assert not source.exists() and target.read_bytes() == b'original scratch video'
            with readonly_planner_session(path) as db:
                actual = next(v.source for v in load_planner_context(db).videos if v.id == 100)
            assert actual == (manifest.payload.actions[0].source if boundary == 'after_fs_verified' else manifest.payload.actions[0].target)
            comparison = session._identity.compare(manifest, manifest.payload.actions[0], action_index=0)
            assert comparison.outcome == 'MATCH' and comparison.target.physical_identity_match == 'MATCH'
            print(json.dumps(dict(paths=paths, pid=os.getpid(), boundary=boundary,
                boundary_assertions_completed=True)), flush=True)
            while True:
                signal.pause()

        def appended(writer, entry):
            writer_seen[:] = [writer]
            append(writer, entry)
            if boundary == 'after_db_reconciled' and entry.state == 'DB_RECONCILED':
                reached()

        def applied(db):
            if boundary == 'after_fs_verified':
                reached()
            apply(db)

        def committed(db):
            commit(db)
            if boundary == 'after_commit':
                reached()

        journal.DurableJournal.append = appended
        database._ScratchVideoDatabase._apply = applied
        database._ScratchVideoDatabase._commit = committed
        session.execute_video_move(manifest, manifest.payload.actions[0], action_index=0)
        raise AssertionError('process never reached requested E5A boundary')


def inspect(paths, boundary, early_exit):
    manifest = manifest_from_json(Path(paths['manifest']).read_text(encoding='utf-8'))
    def read_only(event, args):
        if event == 'open':
            assert not args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND), (event, args)
        if event == 'sqlite3.connect':
            assert 'mode=ro' in str(args[0]), args
        assert event not in {'os.mkdir', 'os.rmdir', 'os.rename', 'os.remove', 'os.link',
            'os.symlink', 'os.truncate', 'os.chmod', 'os.chown', 'os.utime'}, (event, args)
    sys.addaudithook(read_only)
    report = inspect_scratch_action(manifest, manifest.payload.actions[0], action_index=0,
        scratch_root=Path(paths['root']), journal_path=Path(paths['journal']),
        evidence_path=Path(paths['evidence']))
    if early_exit:
        os._exit(0)
    assert report.outcome == 'STOP' and report.manual_review_required
    assert report.identity_continuity == report.target.physical_identity_match == 'UNKNOWN'
    assert report.observed_state == 'TARGET_ONLY' and report.evidence.status == 'VALID'
    expected_phase = 'DB_RECONCILED' if boundary == 'after_db_reconciled' else 'FS_VERIFIED'
    assert report.journal.status == 'VALID' and report.journal.last_action_phase == expected_phase
    assert all(e.state != 'COMPLETE' for e in report.journal.entries)
    uri = 'file:' + quote(paths['database'], safe='/') + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        values = connection.execute('SELECT relative_path,root_folder,filename FROM videos WHERE id=100').fetchone()
    expected = (manifest.payload.actions[0].source, 'old') if boundary == 'after_fs_verified' else (manifest.payload.actions[0].target, 'Show')
    assert values == (*expected, 'Historical release - 01.mkv')
    with readonly_planner_session(Path(paths['database'])) as db:
        context = load_planner_context(db)
    assert next(v.source for v in context.videos if v.id == 100) == expected[0]
    print(json.dumps(dict(pid=os.getpid(), assertions_completed=True, report=asdict(report))), flush=True)


if __name__ == '__main__':
    if sys.argv[1] == 'create':
        create(Path(sys.argv[2]), sys.argv[3])
    else:
        inspect(json.loads(sys.argv[2]), sys.argv[3], sys.argv[4] == 'early_exit')
