"""Production admission is explicit; all filesystem operations here are local fixtures."""
from dataclasses import replace
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import sqlite3
import shutil

import pytest
from sqlalchemy.orm import Session

from app import target_execution_mounts as mounts
from app import target_execution_filesystem as fs
from app.database import make_engine
from app.maintenance import ScratchMaintenance, MaintenanceStopped
from app.target_execution_sequence import ScratchManifestExecution
from app.target_execution_manifest import RuntimeEvidence, WarningAcknowledgement, manifest_to_json
from app.target_execution_cleanup import approve_cleanup
from app.target_execution_guard import complete_reorganization, NormalOperation, _require_no_evidence
from app.models import Video
from test_v6_execution_file_actions import prepare_mixed, generate
from test_v6_execution_cleanup import prepare as prepare_cleanup
from test_v6_p2c_rescan import approve
from test_v6_execution_projection import expected_rows
from v6_database_case import database_rows

SHARE = '//synology.test/Anime'


def test_explicit_media_policy_preserves_scratch_cifs_denial(tmp_path):
    fd = fs._open_absolute(tmp_path)
    try:
        records = mounts._read_mounts()
        current = next(m for m in records if m.mount_id == mounts._mount_id(fd))
        remote = replace(current, point=tmp_path, fs_root=PurePosixPath('/'), fstype='cifs', source=SHARE)
        records = (remote,)
        with pytest.raises(ValueError):
            mounts._check_mounts(tmp_path, fd, records)
        checker = getattr(mounts, '_check_production_mount', None)
        assert checker is not None, 'explicit production media policy is missing'
        assert checker(tmp_path, fd, records, share=SHARE, share_root='/', writable=True) == remote
        with pytest.raises(ValueError):
            checker(tmp_path, fd, records, share='//other/Anime', share_root='/', writable=True)
        readonly = replace(remote, options=('ro',))
        records = tuple(readonly if m == remote else m for m in records)
        assert checker(tmp_path, None, records, share=SHARE, share_root='/', writable=False) == readonly
        with pytest.raises(ValueError):
            checker(tmp_path, fd, records, share=SHARE, share_root='/', writable=True)
    finally:
        os.close(fd)


@pytest.fixture
def production(tmp_path, monkeypatch):
    media, state = tmp_path / 'media', tmp_path / 'state'
    media.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    with ScratchManifestExecution.create(media, database_parent=state) as scratch:
        _, path = prepare_cleanup(scratch)
        root = scratch.root
        source = root / 'old/copy/duplicate.mkv'
        source.write_bytes(b'confirmed secondary video')
        source.chmod(0o600)
        (root / 'seznam-souboru.txt').write_bytes(b'preserved root inventory')
        (root / 'seznam-souboru.txt').chmod(0o600)
        engine = make_engine(f'sqlite:///{path}')
        try:
            with Session(engine) as db:
                primary = db.get(Video, 100)
                db.add(Video(id=101, catalog_title_id=primary.catalog_title_id,
                    catalog_collection_id=primary.catalog_collection_id, relative_path='old/copy/duplicate.mkv',
                    root_folder='old', filename='duplicate.mkv', file_type='episode', content_type_manual='episode',
                    season_episode_number=1, local_episode_number=1, episode_number_manual_override=1,
                    episode_number_verified_at=primary.episode_number_verified_at, duplicate_of_video_id=100,
                    size=source.stat().st_size, mtime_ns=source.stat().st_mtime_ns))
                db.flush()
                from app.hierarchy_evaluation import finalize_collection_hierarchy
                finalize_collection_hierarchy(primary.catalog_collection, list(primary.catalog_collection.videos),
                    preserved_title_ids={primary.catalog_title_id})
                db.commit()
        finally:
            engine.dispose()
    root.rename(media / 'Anime')
    root = media / 'Anime'
    path.rename(state / 'anime.db')
    path = state / 'anime.db'
    # The synthetic tables are the current schema, not a production migration.
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=9')
    (root / 'IMPORT').mkdir(mode=0o700)
    (root / 'IMPORT/uploading.mkv').write_bytes(b'untouched incoming upload')
    (root / 'IMPORT/uploading.mkv').chmod(0o600)
    (root / 'Duplicates').mkdir(exist_ok=True, mode=0o700)
    checker = getattr(mounts, '_check_production_mount', None)
    assert checker is not None, 'production binding policy is missing'
    # Only substitute the remote mount boundary; all E7 operations remain real local I/O.
    def local_media(root_path, fd, records, *, share, share_root, writable):
        assert share == SHARE and share_root == '/'
        if fd is not None:
            record = next(m for m in records if m.mount_id == mounts._mount_id(fd))
        else:
            record = max((m for m in records if Path(root_path).is_relative_to(m.point)), key=lambda m: len(m.point.parts))
        return replace(record, fstype='cifs', source=SHARE, options=('rw',))
    monkeypatch.setattr(mounts, '_check_production_mount', local_media)
    factory = getattr(ScratchManifestExecution, 'open_existing', None)
    assert factory is not None, 'E7 cannot accept an existing library/database'
    owner = factory(root, database_path=path, share=SHARE, share_root='/')
    try:
        manifest = approve(generate(owner, path))
        backup = state / 'backup.sqlite'
        backup.write_bytes(path.read_bytes())
        p = manifest.payload.db_fingerprint
        runtime = RuntimeEvidence(scanner_paused_through_verification=True,
            inventory_writer_paused_through_verification=True, snapshot_capable=True,
            snapshot_id='isolated-fixture-snapshot', snapshot_at=manifest.created_at,
            snapshot_manifest_id=manifest.manifest_id, db_backup_path=str(backup),
            db_backup_sha256=p.sha256, db_backup_size=p.size,
            db_backup_user_version=p.user_version, db_backup_manifest_id=manifest.manifest_id,
            actual_windows_root=manifest.payload.windows_root, mount_identity=owner.mount_identity,
            mount_read_only_now=False, free_bytes=10**12, mount_read_write_checked=True,
            safe_write_capability_proven=True, rename_noreplace_capability_proven=True)
        yield owner, path, manifest, runtime
    finally:
        owner.close()


def test_existing_database_cleanup_normal_and_ro_startup(production, monkeypatch):
    owner, path, manifest, runtime = production
    before = database_rows(path)
    db_identity = (path.stat().st_dev, path.stat().st_ino)
    incoming = (owner.root / 'IMPORT/uploading.mkv').read_bytes()
    selected = []
    def decision(proposal):
        assert all(not p.path.startswith(('IMPORT', 'Duplicates', 'Subs')) for p in proposal.candidates)
        selected.extend(p.path for p in proposal.candidates if p.path == 'old' or p.path.startswith('old/'))
        return approve_cleanup(proposal, tuple(selected), actor='operator', reason='reviewed original empty paths')
    from app.tools.reorganize import execute_approved_reorganization
    runtime_lock = path.parent / 'runtime.lock'
    runtime_lock.write_bytes(b'')
    runtime_lock.chmod(0o600)
    owner.close()
    result = execute_approved_reorganization(database=path, library_root=owner.root, share=SHARE,
        share_root='/', manifest=manifest, approved_sha256=hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest(),
        runtime=runtime, runtime_lock=runtime_lock, cleanup_decision=decision)
    assert result['state'] == 'NORMAL'
    assert selected and all(not (owner.root / p).exists() for p in selected)
    assert (owner.root / 'IMPORT/uploading.mkv').read_bytes() == incoming
    assert (owner.root / 'Duplicates').is_dir()
    secondary = next(a for a in manifest.payload.actions if a.object_kind == 'video' and a.object_id == 101)
    assert secondary.action == 'QUARANTINE'
    assert (owner.root / secondary.target).read_bytes() == b'confirmed secondary video'
    assert database_rows(path) == expected_rows(before, manifest.payload.locator_patches)
    assert (path.stat().st_dev, path.stat().st_ino) == db_identity
    engine = make_engine('sqlite:///' + path.as_uri() + '?mode=rw&uri=true')
    try:
        complete_reorganization(engine, owner.root)
        assert isinstance(_require_no_evidence(path), NormalOperation)
    finally:
        engine.dispose()
    owner.close()
    # A second view of the same logical share has independent directory/file
    # inodes. NORMAL must validate history and the share, not RW-view pins.
    rw_view = owner.root.with_name('completed-rw-view')
    owner.root.rename(rw_view)
    shutil.copytree(rw_view, owner.root, copy_function=shutil.copy2)
    assert owner.root.stat().st_ino != rw_view.stat().st_ino
    observe_media = mounts._check_production_mount
    def ro_view(root_path, fd, records, *, share, share_root, writable):
        if writable:
            raise ValueError('RO application cannot use execution media capability')
        return replace(observe_media(root_path, fd, records, share=share, share_root=share_root,
            writable=False), options=('ro',))
    monkeypatch.setattr(mounts, '_check_production_mount', ro_view)
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_v6_normal_operation import settings_for
    with TestClient(create_app(settings_for(path, owner.root))) as client:
        assert client.get('/health').status_code == 200
        response = client.post('/scan', follow_redirects=False)
        assert response.status_code == 303 and 'error=' not in response.headers['location']
    actual, expected = database_rows(path), expected_rows(before, manifest.payload.locator_patches)
    from app.database import Base
    changes = [(table, [c.name for c, old, new in zip(Base.metadata.tables[table].columns, old_row, new_row) if old != new])
        for table in expected for old_row, new_row in zip(expected[table], actual[table]) if old_row != new_row]
    assert actual == expected, changes
    guard = path.with_name(path.name + '.execution-guard.json')
    guard.rename(guard.with_suffix('.missing'))
    with pytest.raises(MaintenanceStopped):
        _require_no_evidence(path)


@pytest.mark.parametrize('fault', ['approval', 'runtime', 'snapshot', 'windows', 'warnings'])
def test_production_rejection_has_no_media_or_database_effect(production, fault):
    owner, path, manifest, runtime = production
    before = database_rows(path)
    original_paths = sorted(p.relative_to(owner.root).as_posix() for p in owner.root.rglob('*'))
    digest = hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest()
    if fault == 'approval': digest = '0' * 64
    elif fault == 'runtime': runtime = None
    elif fault == 'snapshot': runtime = replace(runtime, snapshot_manifest_id='wrong')
    elif fault == 'windows': runtime = replace(runtime, actual_windows_root=None)
    elif fault == 'warnings':
        manifest = replace(manifest, acknowledgements=(WarningAcknowledgement('0' * 64,
            'unknown', 'video', 100, 'operator', manifest.created_at),))
        digest = hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest()
    with ScratchMaintenance(path, owner.root, production_share=SHARE).maintenance() as lease:
        with pytest.raises(Exception, match='STOP'):
            owner.execute(manifest, maintenance_owner=lease, runtime_evidence=runtime,
                approved_manifest_sha256=digest, cleanup_decision=lambda proposal: None, resume_scanning=True)
    assert database_rows(path) == before
    assert sorted(p.relative_to(owner.root).as_posix() for p in owner.root.rglob('*')) == original_paths
    assert not owner.directory_evidence_path.exists() and not owner.journal_path.exists()


def test_worker_runtime_lock_excludes_second_owner(tmp_path):
    from app.tools import reorganize
    lock = tmp_path / 'runtime.lock'
    lock.write_bytes(b'')
    lock.chmod(0o600)
    with reorganize.runtime_exclusion(lock):
        with pytest.raises(MaintenanceStopped):
            with reorganize.runtime_exclusion(lock):
                pytest.fail('second worker acquired an active runtime lock')


def test_ordinary_production_admission_requires_ro_mount(production, monkeypatch):
    from app import target_execution_guard as guard
    owner, path, _, _ = production
    # Isolate the mount-role decision from terminal history validation, which
    # the complete worker/startup integration exercises independently.
    monkeypatch.setattr(guard, '_require_no_evidence', lambda database: NormalOperation('completed-fixture'))
    engine = make_engine('sqlite:///' + path.as_uri() + '?mode=rw&uri=true')
    try:
        with pytest.raises(MaintenanceStopped):
            with guard.ordinary_scan_admission(engine, owner.root):
                pytest.fail('ordinary production admitted the RW worker view')
    finally:
        engine.dispose()


def test_import_directory_identity_is_pinned_without_traversing_uploads(production):
    from app.target_execution_directories import _DirectoryPreparation
    from app.target_planner_filesystem import inventory_filesystem
    owner, _, manifest, _ = production
    owner._manifest = manifest
    preparation = _DirectoryPreparation(owner, inventory_filesystem(owner.root))
    original = owner.root / 'IMPORT'
    original.rename(owner.root.parent / 'original-import')
    original.mkdir(mode=0o700)
    with pytest.raises(fs.RenameRejected):
        preparation.check_bindings()


@pytest.mark.parametrize('fault', ['symlink', 'wal', 'schema', 'journal', 'guard'])
def test_existing_only_admission_refuses_unsafe_database_or_history(production, fault):
    owner, path, _, _ = production
    if fault == 'symlink':
        target = path.with_name('original.sqlite')
        path.rename(target)
        path.symlink_to(target)
    elif fault == 'wal': path.with_name(path.name + '-wal').write_bytes(b'preserve sidecar')
    elif fault == 'schema':
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA user_version=0')
    elif fault == 'journal': path.with_suffix('.journal.jsonl').write_bytes(b'partial historical journal')
    elif fault == 'guard': path.with_name(path.name + '.execution-guard.json').symlink_to(path.parent / 'missing')
    with pytest.raises((ValueError, OSError)):
        ScratchManifestExecution.open_existing(owner.root, database_path=path, share=SHARE)


@pytest.mark.parametrize('moment', ['rename', 'cleanup', 'normal'])
def test_interrupted_worker_keeps_achieved_state_and_blocks_startup(production, monkeypatch, moment):
    from app.tools import reorganize
    from app.target_execution_sequence import _ExecutionSequence
    owner, path, manifest, runtime = production
    before = database_rows(path)
    owner.close()
    lock = path.parent / 'runtime.lock'
    lock.write_bytes(b'')
    lock.chmod(0o600)
    original_append = _ExecutionSequence._append
    if moment == 'rename':
        def interrupted_append(current, state):
            original_append(current, state)
            if state == 'FS_APPLIED':
                raise OSError('simulated interruption after applied rename')
        monkeypatch.setattr(_ExecutionSequence, '_append', interrupted_append)
    elif moment == 'normal':
        original_complete = reorganize.complete_reorganization
        def interrupted_completion(*args, **kwargs):
            original_complete(*args, **kwargs)
            raise OSError('simulated wrapper failure after NORMAL')
        monkeypatch.setattr(reorganize, 'complete_reorganization', interrupted_completion)
        from app import target_execution_guard as guard_module
        original_revoke = guard_module._revoke_transition
        revoked_under_lock = []
        def checked_revoke(*args):
            try:
                with reorganize.runtime_exclusion(lock):
                    revoked_under_lock.append(False)
            except MaintenanceStopped:
                revoked_under_lock.append(True)
            return original_revoke(*args)
        monkeypatch.setattr(guard_module, '_revoke_transition', checked_revoke)
    def decision(proposal):
        if moment == 'cleanup':
            raise KeyboardInterrupt('simulated operator interruption at cleanup approval')
        selected = tuple(p.path for p in proposal.candidates if p.path == 'old' or p.path.startswith('old/'))
        return approve_cleanup(proposal, selected, actor='operator', reason='explicit test selection')
    with pytest.raises((Exception, KeyboardInterrupt)):
        reorganize.execute_approved_reorganization(database=path, library_root=owner.root, share=SHARE,
            share_root='/', manifest=manifest,
            approved_sha256=hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest(),
            runtime=runtime, runtime_lock=lock, cleanup_decision=decision)
    with pytest.raises(MaintenanceStopped):
        _require_no_evidence(path)
    assert (owner.root / 'IMPORT/uploading.mkv').read_bytes() == b'untouched incoming upload'
    assert (owner.root / 'Duplicates').is_dir()
    if moment == 'rename':
        first = manifest.payload.actions[0]
        assert first.action == 'QUARANTINE'
        assert (owner.root / first.target).is_file() and not (owner.root / first.source).exists()
        assert database_rows(path) == before
    else:
        assert database_rows(path) == expected_rows(before, manifest.payload.locator_patches)
    if moment == 'normal':
        assert revoked_under_lock and all(revoked_under_lock), 'STOP must precede runtime lock release'
