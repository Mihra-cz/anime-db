"""E7 uses the existing maintenance binding across independent local roots."""
from pathlib import Path
import tempfile
import os
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import make_engine
from app.main import create_app
from app.maintenance import ScratchMaintenance, MaintenanceStopped
from app.models import Video
from app.probe import probe_video
from app.scanner import scan_library, LibrarySafetyError
from app.target_execution_guard import NormalOperation, _require_no_evidence
from app.target_execution_manifest import manifest_to_json
from app.target_execution_journal import load_journal
from app.target_execution_identity import _decode_frame
from app.target_execution_sequence import ScratchManifestExecution
from test_physical_naming import attach_metadata, NOW
from app.hierarchy_review import set_manual_title_hierarchy
from app.physical_naming_service import confirm_physical_naming_choice
from test_scanner_reorganization import PROBE, scan
from test_v6_execution_file_actions import generate
from test_v6_p2c_rescan import approve
from test_v6_normal_operation import complete, settings_for
from v6_database_case import database_rows


def test_separate_database_and_library_complete_normal(tmp_path, monkeypatch):
    configured = os.environ.get('ANIMEDB_SPLIT_TEST_PARENT')
    if configured:
        base = Path(tempfile.mkdtemp(prefix='split-', dir=configured))
    else:
        base = tmp_path
    media_parent, storage = base / 'media', base / 'state'
    media_parent.mkdir(mode=0o700)
    storage.mkdir(mode=0o700)
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE)
    with ScratchManifestExecution.create(media_parent, database_parent=storage) as owner:
        root, path = owner.root, owner.database_path
        assert path.parent == storage and root.parent == media_parent
        assert not root.is_relative_to(storage)
        source = root / 'Old Show/Season 1/Release - 01.mkv'
        source.parent.mkdir(parents=True, mode=0o700)
        source.parent.parent.chmod(0o700)
        with source.open('wb') as stream:
            for _ in range(160):
                stream.write(b'x' * 65536)
        source.chmod(0o600)
        info = source.stat()
        engine = make_engine(f'sqlite:///{path}')
        try:
            from app.migrations import migrate_schema_at_startup
            migrate_schema_at_startup(engine)
            with Session(engine) as db:
                assert scan_library(db, root).created == 1
                video = db.scalar(select(Video))
                ident = video.id
                title = video.catalog_title
                set_manual_title_hierarchy(title, season_number=1, season_label=None,
                    part_type='season', sort_order=1, hierarchy_verified=True)
                attach_metadata(title, str(title.id), 'Show')
                confirm_physical_naming_choice(db, title.collection, 'New Show', 'custom', now=NOW)
                video.content_type_manual = 'episode'
                video.episode_number_manual_override = 1
                video.episode_number_verified_at = NOW
                db.commit()
            assert scan(path, root).unchanged == 1
            manifest = approve(generate(owner, path))
            path.with_suffix('.manifest.json').write_text(manifest_to_json(manifest))
            foreign_parent = base / 'foreign'
            foreign_parent.mkdir(mode=0o700)
            foreign_root = foreign_parent / root.name
            foreign_root.mkdir(mode=0o700)
            # A matching generated basename cannot replace the durable binding.
            with pytest.raises(MaintenanceStopped):
                ScratchMaintenance(path, foreign_root)
            with ScratchMaintenance(path, root).maintenance() as lease:
                owner.execute(manifest, maintenance_owner=lease, resume_scanning=True)
            target = root / next(a.target for a in manifest.payload.actions if a.object_kind == 'video')
            assert not source.exists()
            assert (target.stat().st_dev, target.stat().st_ino, target.stat().st_size) == (info.st_dev, info.st_ino, info.st_size)
            assert info.st_size > 8 * 1024 * 1024
            evidence = (owner.journal_path, owner.directory_evidence_path, *owner.evidence_paths)
            assert all(p.parent == storage and p.is_file() for p in evidence)
            assert not list(media_parent.glob('*.json*'))
            assert [e.state for e in load_journal(owner.journal_path, manifest).entries] == [
                'PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE']
            record = _decode_frame(owner.evidence_paths[0].read_bytes())
            assert (record.version, record.mode, record.content) == (2, 'METADATA_RENAME', None)
            assert record.filesystem.st_ino == info.st_ino
            marker = path.with_name(path.name + '.execution-guard.json')
            held = marker.with_name(marker.name + '.held')
            marker.rename(held)
            try:
                with pytest.raises(LibrarySafetyError):
                    scan(path, root)
            finally:
                held.rename(marker)
            with pytest.raises(LibrarySafetyError):
                scan(path, media_parent)
            rows = database_rows(path)
            assert scan(path, root).unchanged == 1
            assert database_rows(path) == rows
            complete(path, root)
            assert isinstance(_require_no_evidence(path), NormalOperation)
            history = {p: p.read_bytes() for p in evidence}
        finally:
            engine.dispose()
    # Only fixture indexing uses synthetic probe data. Known locators now go
    # through the real standard scanner without technical re-probe.
    monkeypatch.setattr('app.scanner.service.probe_video', probe_video)
    with TestClient(create_app(settings_for(path, root))) as client:
        assert client.get('/health').status_code == 200
        response = client.post('/scan', follow_redirects=False)
        assert response.status_code == 303 and 'error=' not in response.headers['location']
    assert scan(path, root).unchanged == 1
    assert database_rows(path) == rows
    assert all(p.read_bytes() == data for p, data in history.items())
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as db:
            assert db.get(Video, ident).relative_path == target.relative_to(root).as_posix()
    finally:
        engine.dispose()


def test_sqlite_path_check_is_independent_of_media_device(tmp_path):
    """Model a distinct media device; this does not claim a real second mount."""
    from types import SimpleNamespace
    from app import target_execution_filesystem as fs
    from app.target_execution_database import _ScratchVideoDatabase
    path = tmp_path / 'isolated.sqlite'
    path.write_bytes(b'private storage path probe')
    path.chmod(0o600)
    parent = fs._open_absolute(tmp_path)
    fd = os.open(path.name, fs._FILE, dir_fd=parent)
    try:
        db = object.__new__(_ScratchVideoDatabase)
        db.path, db._parent, db._fd = path, parent, fd
        db._storage_device = os.fstat(parent).st_dev
        db._storage_mount = fs._mount_id(parent)
        db._filesystem = SimpleNamespace(_check_root=lambda: None,
            _root_identity=(db._storage_device + 1, 1),
            _mount_id=db._storage_mount + 1, _mounts=fs._read_mounts())
        db._check_path()
        db._storage_mount += 1
        with pytest.raises(ValueError, match='mount binding changed'):
            db._check_path()
    finally:
        os.close(fd)
        os.close(parent)


@pytest.mark.parametrize('separate', [True, False], ids=['separate', 'adjacent'])
def test_approved_cleanup_preserves_files_and_database_binding(tmp_path, separate):
    from app.database import Base
    from app.target_execution_cleanup import approve_cleanup
    from test_physical_naming import collection, title
    from test_v6_execution_cleanup import records
    from test_v6_execution_projection import expected_rows

    media_parent, state_parent = tmp_path / 'media', tmp_path / 'state'
    media_parent.mkdir(mode=0o700)
    state_parent.mkdir(mode=0o700)
    with ScratchManifestExecution.create(media_parent,
            database_parent=state_parent if separate else None) as owner:
        root, path = owner.root, owner.database_path
        storage = state_parent if separate else media_parent
        assert path.parent == storage
        if separate:
            assert not root.is_relative_to(storage)
        source = root / 'old/moved/source.mkv'
        source.parent.mkdir(parents=True, mode=0o700)
        source.parent.parent.chmod(0o700)
        source.write_bytes(b'original cleanup video')
        source.chmod(0o600)
        (root / 'old/empty').mkdir(mode=0o700)
        (root / 'old/retained').mkdir(mode=0o700)
        (root / 'seznam-souboru.txt').write_bytes(b'preserved inventory')
        (root / 'seznam-souboru.txt').chmod(0o600)
        info = source.stat()
        engine = make_engine(f'sqlite:///{path}')
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as db:
                catalog = collection(db)
                main = title(db, catalog, metadata=False)
                main.relative_root_path = 'Show/Season 01'
                attach_metadata(main, str(main.id), 'Show')
                db.add(Video(id=100, catalog_title=main, catalog_collection=catalog,
                    relative_path='old/moved/source.mkv', root_folder='old',
                    filename='Historical release - 01.mkv', size=info.st_size,
                    mtime_ns=info.st_mtime_ns, file_type='episode',
                    season_episode_number=1, local_episode_number=1,
                    episode_number_manual_override=1, episode_number_verified_at=NOW,
                    content_type_manual='episode'))
                db.commit()
        finally:
            engine.dispose()
        path.chmod(0o600)
        manifest = approve(generate(owner, path))
        immutable = manifest_to_json(manifest)
        original = database_rows(path)
        files = {a.target: (root / a.source).read_bytes() for a in manifest.payload.actions}
        approved = {'old/moved', 'old/empty'}
        observed = {}

        def decide(proposal):
            assert approved <= {candidate.path for candidate in proposal.candidates}
            assert [entry.state for entry in owner._writer.inspect_live(manifest)] == [
                'PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'] * len(manifest.payload.actions)
            assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
            observed['directories'] = {entry.path for entry in proposal.before_snapshot if entry.kind == 'directory'}
            observed['db'] = path.read_bytes()
            evidence = (owner.journal_path, owner.directory_evidence_path, *owner.evidence_paths,
                path.with_name(path.name + '.execution-guard.json'))
            observed['history'] = {p: p.read_bytes() for p in evidence}
            return approve_cleanup(proposal, tuple(sorted(approved)), actor='operator',
                reason='approved empty source directories in isolated local test')

        with ScratchMaintenance(path, root).maintenance() as lease:
            result = owner.execute(manifest, maintenance_owner=lease, cleanup_decision=decide)
        assert len(result) == len(manifest.payload.actions)
        assert not source.exists()
        target = root / next(a.target for a in manifest.payload.actions if a.object_kind == 'video')
        assert (target.stat().st_dev, target.stat().st_ino) == (info.st_dev, info.st_ino)
        assert {name: (root / name).read_bytes() for name in files} == files
        remaining = {'.'} | {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_dir()}
        assert remaining == observed['directories'] - approved
        assert (root / 'old/retained').is_dir()
        assert set(owner.cleanup_outcome.removed) == approved
        assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
        assert path.read_bytes() == observed['db']
        assert manifest_to_json(manifest) == immutable
        assert [entry.state for entry in load_journal(owner.journal_path, manifest).entries] == [
            'PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'] * len(manifest.payload.actions)
        evidence = records(owner)
        assert evidence[-1]['state'] == 'COMPLETE'
        assert {entry['path'] for entry in evidence if entry['state'] == 'VERIFIED'} == approved
        history = {**observed['history'], owner.cleanup_evidence_path: owner.cleanup_evidence_path.read_bytes()}
        assert all(p.parent == storage for p in history)
        assert all(p.read_bytes() == data for p, data in history.items())
        assert not list(root.rglob('*.json*'))
    assert all(p.read_bytes() == data for p, data in history.items())
    assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
