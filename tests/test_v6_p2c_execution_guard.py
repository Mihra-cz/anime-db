"""R3: ordinary scans stop before effects; only private synthetic SQLite/files."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy.orm import Session

from app.database import make_engine
from app.maintenance import ScratchMaintenance
from app.scanner import LibrarySafetyError, scan_library
from app.target_execution_sequence import ScratchManifestExecution
from test_v6_execution_file_actions import prepare_mixed, generate
from test_v6_p2c_rescan import approve
from v6_database_case import database_rows


def guard_path(path):
    return path.with_name(path.name + '.execution-guard.json')


@pytest.fixture
def case(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        _, path = prepare_mixed(owner)
        # Defeat the older basename divergence guard: the crash must be stopped
        # by R3 even when every OLD locator still matches its source filename.
        with closing(sqlite3.connect(path)) as db, db:
            for ident, locator in db.execute('SELECT id,relative_path FROM videos').fetchall():
                db.execute('UPDATE videos SET filename=? WHERE id=?', (Path(locator).name, ident))
        manifest = approve(generate(owner, path))
        yield owner, path, manifest


@pytest.fixture
def small_case(tmp_path):
    from v6_database_case import prepare_database
    with ScratchManifestExecution.create(tmp_path) as owner:
        _, _, _, path = prepare_database(owner)
        with closing(sqlite3.connect(path)) as db, db:
            for ident, locator in db.execute('SELECT id,relative_path FROM videos').fetchall():
                db.execute('UPDATE videos SET filename=? WHERE id=?', (Path(locator).name, ident))
        yield owner, path, approve(generate(owner, path))


def snapshot(root):
    result = {}
    for path in (root, *root.rglob('*')):
        info = path.lstat()
        result[path.relative_to(root).as_posix()] = (info.st_ino, info.st_mode,
            info.st_size, info.st_mtime_ns, path.read_bytes() if path.is_file() else None)
    return result


def refused(path, root, confirm=True, maintenance=None):
    engine = make_engine(f'sqlite:///{path}', maintenance=maintenance)
    try:
        with Session(engine) as session:
            with pytest.raises(LibrarySafetyError, match='[Rr]eorgani') as stopped:
                scan_library(session, root, confirm_deletions=confirm)
            assert not stopped.value.confirmation_allowed
    finally:
        engine.dispose()


def test_guard_is_durable_before_directory_preparation_and_move(case, monkeypatch):
    from app import target_execution_directories as directories
    owner, path, manifest = case
    before = database_rows(path)
    def boundary(preparation):
        assert guard_path(path).is_file(), 'R3 guard missing before first filesystem effect'
        raise RuntimeError('synthetic STOP before any directory/MOVE')
    monkeypatch.setattr(directories._DirectoryPreparation, 'prepare', boundary)
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(Exception, match='synthetic STOP'):
            owner.execute(manifest, maintenance_owner=lease)
    assert database_rows(path) == before
    refused(path, owner.root)


@pytest.mark.parametrize('confirm', [False, True])
def test_interrupted_move_repeated_scan_preserves_every_row_and_file(small_case, monkeypatch, confirm):
    from app import target_execution_database as database
    owner, path, manifest = small_case
    before = database_rows(path)
    apply = database._ScratchSequenceDatabase._apply
    def stop(current, index):
        if manifest.payload.actions[index].action != 'KEEP':
            raise RuntimeError('synthetic interruption after MOVE before DB update')
        return apply(current, index)
    monkeypatch.setattr(database._ScratchSequenceDatabase, '_apply', stop)
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(Exception, match='synthetic interruption'):
            owner.execute(manifest, maintenance_owner=lease)
    assert database_rows(path) == before
    physical, content = snapshot(owner.root), path.read_bytes()
    for _ in range(2):
        refused(path, owner.root, confirm)
        assert database_rows(path) == before and path.read_bytes() == content
        assert snapshot(owner.root) == physical


@pytest.mark.parametrize('completion', [False, True])
def test_complete_journal_and_live_r2_success_do_not_release_ordinary_guard(small_case, completion):
    owner, path, manifest = small_case
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        owner.execute(manifest, maintenance_owner=lease, completion_rescan=completion)
    assert guard_path(path).is_file()
    before, physical = database_rows(path), snapshot(owner.root)
    refused(path, owner.root)
    assert database_rows(path) == before and snapshot(owner.root) == physical
    if completion:
        assert owner.completion_rescan_result.unchanged == 2


def test_ordinary_library_without_execution_still_scans(tmp_path, monkeypatch):
    from app.database import Base
    from test_scanner import PROBE_RESULT
    root, path = tmp_path / 'library', tmp_path / 'ordinary.sqlite'
    root.mkdir()
    (root / 'Show - 01.mkv').write_bytes(b'ordinary media')
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE_RESULT)
    engine = make_engine(f'sqlite:///{path}')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            assert scan_library(db, root).created == 1
            assert scan_library(db, root, confirm_deletions=True).unchanged == 1
        assert len(database_rows(path)['videos']) == 1
        assert not guard_path(path).exists()
    finally:
        engine.dispose()


@pytest.mark.parametrize('damage', ['empty', 'partial', 'foreign', 'symlink', 'directory', 'missing'])
def test_damaged_or_missing_guard_with_known_execution_never_admits_scan(case, monkeypatch, damage):
    from app import target_execution_database as database
    owner, path, manifest = case
    def stop(*args):
        raise RuntimeError('injected before DB update')
    monkeypatch.setattr(database._ScratchSequenceDatabase, '_apply', stop)
    with ScratchMaintenance(path, owner.root).maintenance() as lease, pytest.raises(Exception):
        owner.execute(manifest, maintenance_owner=lease)
    marker = guard_path(path)
    assert marker.exists()
    if damage == 'empty':
        marker.write_bytes(b'')
    elif damage == 'partial':
        marker.write_bytes(b'{"schema":')
    elif damage == 'foreign':
        marker.write_text('{"database":"/unrelated.sqlite","COMPLETE":true}\n')
    else:
        marker.unlink()
        if damage == 'symlink':
            marker.symlink_to(marker.with_suffix('.missing'))
        elif damage == 'directory':
            marker.mkdir()
    before, physical, content = database_rows(path), snapshot(owner.root), path.read_bytes()
    gate = ScratchMaintenance(path, owner.root)
    for configured in (None, gate):
        refused(path, owner.root, maintenance=configured)
    assert database_rows(path) == before and path.read_bytes() == content
    assert snapshot(owner.root) == physical


@pytest.mark.parametrize('failure', ['open', 'write', 'file_fsync', 'directory_fsync', 'collision'])
def test_guard_creation_fault_never_reaches_mkdir_or_move(case, monkeypatch, failure):
    from app import target_execution_guard as guards
    owner, path, manifest = case
    gate = ScratchMaintenance(path, owner.root)
    before, physical = database_rows(path), snapshot(owner.root)
    marker = guard_path(path)
    if failure == 'collision':
        marker.write_bytes(b'existing unrelated bytes must survive')
    original_open, original_write, original_sync = os.open, os.write, os.fsync
    def is_guard(fd):
        return os.readlink(f'/proc/self/fd/{fd}') == str(marker)
    def opened(name, flags, *args, **kwargs):
        if failure == 'open' and name == marker.name:
            raise OSError('injected guard open failure')
        return original_open(name, flags, *args, **kwargs)
    def written(fd, data):
        if failure == 'write' and is_guard(fd):
            original_write(fd, data[:7])
            raise OSError('injected partial guard write')
        return original_write(fd, data)
    def synced(fd):
        if (failure == 'file_fsync' and is_guard(fd)
                or failure == 'directory_fsync' and marker.exists()
                and os.readlink(f'/proc/self/fd/{fd}') == str(marker.parent)):
            raise OSError('injected guard fsync failure')
        return original_sync(fd)
    with gate.maintenance() as lease:
        with monkeypatch.context() as fault:
            fault.setattr(guards.os, 'open', opened)
            fault.setattr(guards.os, 'write', written)
            fault.setattr(guards.os, 'fsync', synced)
            with pytest.raises(Exception) as stopped:
                owner.execute(manifest, maintenance_owner=lease)
            assert stopped.value.phase == 'EXECUTION_GUARD'
    assert database_rows(path) == before and snapshot(owner.root) == physical
    assert not owner.journal_path.exists() and not owner.directory_evidence_path.exists()
    if failure == 'collision':
        assert marker.read_bytes() == b'existing unrelated bytes must survive'
    if failure != 'open':
        refused(path, owner.root)


def test_file_and_parent_fsync_acknowledged_before_first_library_effect(case, monkeypatch):
    import json
    import stat
    from app import target_execution_guard as guards
    from app import target_execution_directories as directories
    owner, path, manifest = case
    gate = ScratchMaintenance(path, owner.root)
    acknowledgements = []
    sync = os.fsync
    def synced(fd):
        sync(fd)
        if guard_path(path).exists():
            location = os.readlink(f'/proc/self/fd/{fd}')
            if location in {str(guard_path(path)), str(path.parent)}:
                acknowledgements.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
    def boundary(preparation):
        assert acknowledgements == ['file', 'directory']
        value = json.loads(guard_path(path).read_text())
        assert value['database'] == str(path) and value['library'] == str(owner.root)
        assert value['manifest_id'] == manifest.manifest_id
        assert value['database_identity'] == [path.stat().st_dev, path.stat().st_ino]
        raise RuntimeError('checked ordering')
    with gate.maintenance() as lease:
        monkeypatch.setattr(guards.os, 'fsync', synced)
        monkeypatch.setattr(directories._DirectoryPreparation, 'prepare', boundary)
        with pytest.raises(Exception, match='checked ordering'):
            owner.execute(manifest, maintenance_owner=lease)


def test_plain_scanner_discovers_boundary_and_holds_it_through_commit(case, monkeypatch):
    from app.maintenance import MaintenanceBusy
    from app.scanner import service
    owner, path, _ = case
    gate = ScratchMaintenance(path, owner.root)
    def whole_scan(*args, **kwargs):
        with pytest.raises(MaintenanceBusy):
            with gate.maintenance():
                pytest.fail('exclusive execution admitted during ordinary scan')
        # Exercise an actual transaction/commit inside the scanner admission.
        args[0].connection()
        args[0].commit()
        with pytest.raises(MaintenanceBusy):
            with gate.maintenance():
                pytest.fail('scan boundary released before returning')
        return service.ScanResult()
    monkeypatch.setattr(service, '_scan_library', whole_scan)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as db:
            scan_library(db, owner.root)
        with gate.maintenance(), Session(engine) as db, pytest.raises(MaintenanceBusy):
            scan_library(db, owner.root)
    finally:
        engine.dispose()


def test_guard_before_sql_preserves_pending_caller_transaction(case):
    from app.models import Video
    owner, path, _ = case
    guard_path(path).write_bytes(b'damaged execution evidence')
    before = database_rows(path)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as db:
            video = db.get(Video, 100)
            original = video.filename
            video.filename = 'pending caller edit'
            with pytest.raises(LibrarySafetyError, match='Reorganization'):
                scan_library(db, owner.root, confirm_deletions=True)
            assert db.in_transaction() and video.filename == 'pending caller edit'
            assert database_rows(path) == before
            db.rollback()
            assert video.filename == original
    finally:
        engine.dispose()


@pytest.mark.parametrize('alias', ['relative', 'symlink', 'uri', 'uri_upper', 'uri_numeric', 'uri_localhost'])
def test_sqlite_path_alias_cannot_bypass_persistent_guard(case, alias, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    owner, path, _ = case
    guard_path(path).write_bytes(b'incomplete')
    if alias == 'relative':
        monkeypatch.chdir(path.parent)
        url = f'sqlite:///{path.name}'
    elif alias == 'symlink':
        link = path.with_name('alias.sqlite')
        link.symlink_to(path)
        url = f'sqlite:///{link}'
    else:
        uri = 'True' if alias == 'uri_upper' else '1' if alias == 'uri_numeric' else 'true'
        prefix = 'file://localhost' if alias == 'uri_localhost' else 'file:'
        url = f'sqlite:///{prefix}{path}?mode=rw&uri={uri}'
    engine = make_engine(url)
    try:
        with Session(engine) as db, pytest.raises(LibrarySafetyError, match='Reorganization'):
            scan_library(db, owner.root)
    finally:
        engine.dispose()


@pytest.mark.parametrize('stage', ['begun', 'crash'])
def test_sigkill_then_new_plain_scanner_stops(tmp_path, stage):
    import json
    import signal
    import subprocess
    import sys
    # No inherited owner, Python globals, engine or advisory lease.
    helper = Path(__file__).with_name('v6_execution_guard_helper.py')
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path.cwd()), str(helper.parent))))
    killed = subprocess.run([sys.executable, str(helper), stage, str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=90)
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    info = json.loads(killed.stdout)
    path, root = Path(info['database']), Path(info['root'])
    assert path.read_bytes() == (path.parent / 'before.sqlite.bytes').read_bytes()
    assert guard_path(path).exists()
    if stage == 'crash':
        first = next(a for a in info['actions'] if a['action'] != 'KEEP')
        assert first['action'] == 'MOVE'
        assert not (root / first['source']).exists() and (root / first['target']).is_file()
    before, content, physical = database_rows(path), path.read_bytes(), snapshot(root)
    # Demonstrate that OS locks really were released by SIGKILL.
    with ScratchMaintenance(path, root).maintenance():
        pass
    for confirm in ('false', 'true'):
        scan = subprocess.run([sys.executable, str(helper), 'scan', str(path), str(root), confirm],
            env=env, capture_output=True, text=True, timeout=20)
        assert scan.returncode == 0, scan.stdout + scan.stderr
    assert database_rows(path) == before and path.read_bytes() == content
    assert snapshot(root) == physical


def test_startup_refuses_known_guard_before_migration_or_artwork(case):
    from app.config import Settings
    from app.main import create_app
    from app.target_execution_guard import ExecutionGuardStopped
    owner, path, _ = case
    marker = guard_path(path)
    marker.write_bytes(b'{')
    before = path.read_bytes()
    artwork = path.parent / 'new-artwork'
    settings = Settings(anime_path=owner.root, database_url=f'sqlite:///{path}',
        metadata_enabled=False, metadata_download_artwork=False, metadata_artwork_directory=artwork)
    with pytest.raises(ExecutionGuardStopped, match='Reorganization'):
        create_app(settings)
    assert not artwork.exists() and path.read_bytes() == before


def test_existing_web_app_scan_refuses_guard_but_gets_remain_available(small_case, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app import target_execution_database as database
    owner, path, manifest = small_case
    settings = Settings(anime_path=owner.root, database_url=f'sqlite:///{path}',
        metadata_enabled=False, metadata_download_artwork=False,
        metadata_artwork_directory=path.parent / 'artwork')
    web = create_app(settings)
    apply = database._ScratchSequenceDatabase._apply
    def stop(current, index):
        if manifest.payload.actions[index].action != 'KEEP':
            raise RuntimeError('injected after MOVE')
        return apply(current, index)
    monkeypatch.setattr(database._ScratchSequenceDatabase, '_apply', stop)
    with ScratchMaintenance(path, owner.root).maintenance() as lease, pytest.raises(Exception):
        owner.execute(manifest, maintenance_owner=lease)
    before, physical, content = database_rows(path), snapshot(owner.root), path.read_bytes()
    # Deliberately no lifespan: exercise an app that was already running when
    # its cooperating executor died, separately from the new-startup STOP test.
    client = TestClient(web)
    try:
        for confirm in ('false', 'true'):
            response = client.post('/scan', data={'confirm_deletions': confirm}, follow_redirects=False)
            assert response.status_code == 303
            assert 'Reorganization' in response.headers['location']
            assert 'confirm_deletions=true' not in response.headers['location']
        for route in ('/health', '/', '/hierarchy-review'):
            assert client.get(route).status_code == 200
    finally:
        client.close()
    assert database_rows(path) == before and path.read_bytes() == content
    assert snapshot(owner.root) == physical


def test_first_plain_scan_establishes_scratch_boundary_before_executor_can_enter(small_case, monkeypatch):
    from app.maintenance import MaintenanceBusy
    from app.scanner import service
    owner, path, _ = small_case
    lock = path.with_name(path.name + '.maintenance.lock')
    assert not lock.exists()
    def scan_work(*args, **kwargs):
        gate = ScratchMaintenance(path, owner.root)
        with pytest.raises(MaintenanceBusy):
            with gate.maintenance():
                pytest.fail('executor entered while a first ordinary scan was active')
        return service.ScanResult()
    monkeypatch.setattr(service, '_scan_library', scan_work)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as db:
            scan_library(db, owner.root)
    finally:
        engine.dispose()


def test_hardlinked_database_alias_cannot_hide_guard(case):
    owner, path, _ = case
    guard_path(path).write_bytes(b'incomplete')
    alias = path.with_name('hardlink.sqlite')
    os.link(path, alias)
    refused(alias, owner.root)


def test_live_guard_tamper_after_preparation_stops_before_any_move(small_case, monkeypatch):
    from app import target_execution_directories as directories
    owner, path, manifest = small_case
    before, content = database_rows(path), path.read_bytes()
    prepare = directories._DirectoryPreparation.prepare
    def tampered(current):
        prepare(current)
        guard_path(path).write_bytes(b'{"forged":"replacement evidence"}\n')
    monkeypatch.setattr(directories._DirectoryPreparation, 'prepare', tampered)
    with ScratchMaintenance(path, owner.root).maintenance() as lease, pytest.raises(Exception):
        owner.execute(manifest, maintenance_owner=lease)
    assert database_rows(path) == before and path.read_bytes() == content
    for action in manifest.payload.actions:
        assert (owner.root / action.source).exists()
        if action.action != 'KEEP':
            assert not (owner.root / action.target).exists()
    refused(path, owner.root)


def test_lifespan_rechecks_marker_created_after_app_construction(small_case):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app.target_execution_guard import ExecutionGuardStopped
    owner, path, _ = small_case
    settings = Settings(anime_path=owner.root, database_url=f'sqlite:///{path}',
        metadata_enabled=False, metadata_download_artwork=False,
        metadata_artwork_directory=path.parent / 'artwork')
    web = create_app(settings)
    guard_path(path).write_bytes(b'incomplete execution')
    before = path.read_bytes()
    with pytest.raises(ExecutionGuardStopped, match='Reorganization'):
        with TestClient(web):
            pytest.fail('startup entered with a persisted execution guard')
    assert path.read_bytes() == before


def test_known_e1_without_marker_or_lock_cannot_be_hidden_by_different_scan_root(small_case, monkeypatch):
    from app.target_execution_journal import DurableJournal
    owner, path, manifest = small_case
    with DurableJournal.create(owner.journal_path, manifest):
        pass
    other = path.parent / 'other-library'
    other.mkdir(mode=0o700)
    def forbidden(*args, **kwargs):
        pytest.fail('ordinary fallback entered despite known original E1 evidence')
    monkeypatch.setattr('app.scanner.service._scan_library', forbidden)
    refused(path, other)
