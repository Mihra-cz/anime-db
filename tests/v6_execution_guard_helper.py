"""Fresh interpreter crash/scanner probes; caller supplies private /tmp only."""
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys

from sqlalchemy.orm import Session

from app.database import make_engine
from app.maintenance import ScratchMaintenance
from app.scanner import scan_library, LibrarySafetyError



def nas_probe(scratch):
    """Block only synthetic root metadata; parent observes which PID touched it."""
    from app.database import Base
    from app.scanner import LibraryUnavailableError
    scratch = Path(scratch)
    nas = scratch / 'blocked-cifs-root'
    database = scratch / 'data' / 'anime.db'
    engine = make_engine(f'sqlite:///{database}')
    Base.metadata.create_all(engine)
    engine.dispose()
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('PRAGMA user_version=9')
    original = database.read_bytes()
    os.environ.update(ANIME_PATH=str(nas), DATABASE_URL='sqlite://',
        METADATA_ARTWORK_DIRECTORY=str(scratch / 'global-artwork'),
        METADATA_DOWNLOAD_ARTWORK='false')
    parent_pid = os.getpid()
    reader, writer = os.pipe()
    def wrap(method, function):
        def checked(path, *args, **kwargs):
            if path == nas or path.is_relative_to(nas):
                event = 'PARENT_ROOT_FS' if os.getpid() == parent_pid else 'WORKER_ROOT_FS'
                print(json.dumps({'event': event, 'method': method, 'pid': os.getpid()}), flush=True)
                os.read(reader, 1)  # Only the existing healthcheck timeout may end this worker.
                raise AssertionError('synthetic blocked NAS metadata returned')
            return function(path, *args, **kwargs)
        return checked
    for name in ('resolve', 'stat', 'lstat'):
        setattr(Path, name, wrap(name, getattr(Path, name)))
    from app.main import create_app
    from app.config import Settings
    from fastapi.testclient import TestClient
    from urllib.parse import unquote
    settings = Settings(anime_path=nas, database_url=f'sqlite:///{database}',
        metadata_enabled=False, metadata_download_artwork=False,
        metadata_artwork_directory=scratch / 'artwork', library_access_timeout_seconds=.1)
    web = create_app(settings)
    print(json.dumps({'event': 'APP', 'pid': os.getpid()}), flush=True)
    with TestClient(web) as client:
        assert client.get('/health').status_code == 200
        print(json.dumps({'event': 'STARTUP'}), flush=True)
        response = client.post('/scan', follow_redirects=False)
        assert response.status_code == 303 and 'timeout' in unquote(response.headers['location'])
        print(json.dumps({'event': 'POST_TIMEOUT'}), flush=True)
        with web.state.sessions() as session:
            try:
                scan_library(session, nas, library_access_timeout_seconds=.1)
            except LibraryUnavailableError as exc:
                assert 'timeout' in str(exc)
            else:
                raise AssertionError('blocked NAS did not reach the scanner timeout')
        print(json.dumps({'event': 'DIRECT_TIMEOUT'}), flush=True)
    assert database.read_bytes() == original
    os.close(reader)
    os.close(writer)
    print(json.dumps({'event': 'DONE'}), flush=True)


def main():
    mode, *arguments = sys.argv[1:]
    if mode == 'nas_probe':
        return nas_probe(arguments[0])
    if mode in {'hold_writer', 'try_writer'}:
        from app.maintenance import MaintenanceBusy
        database, root = map(Path, arguments)
        try:
            with ScratchMaintenance(database, root).maintenance():
                print(json.dumps({'event': 'HELD'}), flush=True)
                if mode == 'try_writer':
                    raise AssertionError('exclusive writer bypassed active scanner admission')
                assert sys.stdin.readline().strip() == 'RELEASE'
        except MaintenanceBusy:
            if mode != 'try_writer':
                raise
            print(json.dumps({'event': 'BUSY'}), flush=True)
        return 0
    if mode == 'paused_scan':
        from app import target_execution_guard as guard
        from app.scanner import service
        from test_scanner import PROBE_RESULT
        database, root = map(Path, arguments)
        checked = guard._require_no_evidence
        count = 0
        def pause(*args):
            nonlocal count
            checked(*args)
            count += 1
            if count == 1:
                print(json.dumps({'event': 'CHECKED'}), flush=True)
                assert sys.stdin.readline().strip() == 'CONTINUE'
        guard._require_no_evidence = pause
        service.probe_video = lambda *args, **kwargs: PROBE_RESULT
        engine = make_engine(f'sqlite:///{database}')
        try:
            with Session(engine) as db:
                try:
                    result = scan_library(db, root, confirm_deletions=True)
                except LibrarySafetyError as exc:
                    print(json.dumps({'event': 'STOP', 'reason': str(exc)}), flush=True)
                else:
                    print(json.dumps({'event': 'SCANNED', 'created': result.created,
                        'removed': result.removed}), flush=True)
        finally:
            engine.dispose()
        return 0
    if mode in {'crash', 'begun', 'crash_wait'}:
        from app import target_execution_database as database
        from app.target_execution_sequence import ScratchManifestExecution
        from test_v6_execution_file_actions import generate
        from test_v6_p2c_rescan import approve
        from v6_database_case import prepare_database
        with ScratchManifestExecution.create(Path(arguments[0])) as owner:
            _, _, _, path = prepare_database(owner)
            with closing(sqlite3.connect(path)) as db, db:
                for ident, locator in db.execute('SELECT id,relative_path FROM videos').fetchall():
                    db.execute('UPDATE videos SET filename=? WHERE id=?', (Path(locator).name, ident))
            manifest = approve(generate(owner, path))
            gate = ScratchMaintenance(path, owner.root)
            original = path.read_bytes()
            (path.parent / 'before.sqlite.bytes').write_bytes(original)
            print(json.dumps({'database': str(path), 'root': str(owner.root),
                'actions': [{'source': a.source, 'target': a.target, 'action': a.action}
                    for a in manifest.payload.actions]}), flush=True)
            if mode == 'crash_wait':
                assert sys.stdin.readline().strip() == 'EXECUTE'
            if mode in {'crash', 'crash_wait'}:
                apply = database._ScratchSequenceDatabase._apply
                def killed(current, index):
                    if manifest.payload.actions[index].action != 'KEEP':
                        os.kill(os.getpid(), signal.SIGKILL)
                    return apply(current, index)
                database._ScratchSequenceDatabase._apply = killed
            else:
                from app import target_execution_directories as directories
                def killed(*args):
                    os.kill(os.getpid(), signal.SIGKILL)
                directories._DirectoryPreparation.prepare = killed
            with gate.maintenance() as lease:
                owner.execute(manifest, maintenance_owner=lease)
        raise AssertionError('crash hook did not fire')
    if mode == 'scan':
        path, root, confirm = arguments
        engine = make_engine(f'sqlite:///{path}')
        try:
            with Session(engine) as db:
                try:
                    scan_library(db, Path(root), confirm_deletions=confirm == 'true')
                except LibrarySafetyError as exc:
                    print(str(exc), flush=True)
                    return 0 if 'Reorganization' in str(exc) else 4
                raise AssertionError('ordinary scanner passed a persisted execution')
        finally:
            engine.dispose()
    raise AssertionError(mode)


if __name__ == '__main__':
    raise SystemExit(main())
