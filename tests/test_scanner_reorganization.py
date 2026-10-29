"""Real SOURCE -> TARGET execution followed by the ordinary write-capable scanner."""
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app.hierarchy_review import set_manual_title_hierarchy
from app.maintenance import ScratchMaintenance
from app.models import Video, ExternalSubtitle, VideoVariantGroup
from app.physical_naming_service import confirm_physical_naming_choice
from app.scanner import scan_library, LibrarySafetyError
from app.target_execution_sequence import ScratchManifestExecution
from test_physical_naming import attach_metadata, NOW
from test_v6_execution_file_actions import generate
from test_v6_p2c_rescan import approve
from test_v6_p2c_execution_guard import small_case
from v6_database_case import database_rows

PROBE = dict(duration=60.0, video_codec='h264', width=320, height=240,
    audio=[dict(stream_index=1, codec='aac', language='jpn')],
    subtitles=[dict(stream_index=2, codec='ass', language='eng', title=None)])


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE)
    with ScratchManifestExecution.create(tmp_path) as owner:
        for number in range(1, 4):
            video = owner.root / f'Old Show/Season 1/Release - {number:02}.mkv'
            video.parent.mkdir(parents=True, exist_ok=True)
            video.write_bytes(b'synthetic media')
            video.with_suffix('.cs.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nAhoj, jak se mas?\n')
        path = owner.database_path
        engine = make_engine(f'sqlite:///{path}')
        from app.migrations import migrate_schema_at_startup
        migrate_schema_at_startup(engine)
        with Session(engine) as db:
            assert scan_library(db, owner.root).created == 3
            videos = db.scalars(select(Video).order_by(Video.id)).all()
            title = videos[0].catalog_title
            set_manual_title_hierarchy(title, season_number=1, season_label=None,
                part_type='season', sort_order=1, hierarchy_verified=True)
            attach_metadata(title, str(title.id), 'Show')
            confirm_physical_naming_choice(db, title.collection, 'New Show', 'custom', now=NOW)
            confirm_physical_naming_choice(db, title, 'Custom', 'custom', now=NOW)
            title.manual_display_title = 'Human title'
            variant = VideoVariantGroup(catalog_title=title, manual_label='BD', verified_at=NOW)
            for number, video in enumerate(videos, 1):
                video.video_variant_group = variant
                video.content_type_manual = 'episode'
                video.episode_number_manual_override = number
                video.episode_number_verified_at = NOW
                video.audio_tracks[0].manual_language = 'cs'
                video.internal_subtitles[0].manual_language = 'sk'
            for sub in db.scalars(select(ExternalSubtitle)):
                sub.manual_language = 'sk'
            db.commit()
        engine.dispose()
        for directory in owner.root.rglob('*'):
            if directory.is_dir():
                directory.chmod(0o700)
            else:
                directory.chmod(0o600)
        manifest = approve(generate(owner, path))
        assert not manifest.payload.generation_diagnostics
        assert any(a.action == 'MOVE' for a in manifest.payload.actions)
        yield owner, path, manifest


def scan(path, root, **kwargs):
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as db:
            return scan_library(db, root, **kwargs)
    finally:
        engine.dispose()


def execute(case, **kwargs):
    owner, path, manifest = case
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        return owner.execute(manifest, maintenance_owner=lease, **kwargs)


def test_real_move_then_ordinary_scan_preserves_rows_and_is_idempotent(case):
    owner, path, manifest = case
    from test_v6_execution_projection import expected_rows
    original = database_rows(path)
    contents = {a.action_id: (owner.root / a.source).read_bytes() for a in manifest.payload.actions}
    execute(case, resume_scanning=True)
    before = database_rows(path)
    assert before == expected_rows(original, manifest.payload.locator_patches)
    for action in manifest.payload.actions:
        assert (owner.root / action.target).read_bytes() == contents[action.action_id]
        if action.action != 'KEEP':
            assert not (owner.root / action.source).exists()
    for _ in range(2):
        result = scan(path, owner.root)
        assert vars(result) == dict(found=3, created=0, updated=0, unchanged=3, removed=0, errors=0)
        assert database_rows(path) == before
    assert path.with_name(path.name + '.execution-guard.json').is_file()
    assert owner.journal_path.is_file()


def test_new_unrelated_video_and_reprobe_keep_existing_authority(case):
    owner, path, manifest = case
    execute(case, resume_scanning=True)
    known = next(a for a in manifest.payload.actions if a.object_kind == 'video')
    engine = make_engine(f'sqlite:///{path}')
    fields = ('id', 'catalog_title_id', 'catalog_collection_id', 'filename', 'file_type',
        'content_type_manual', 'episode_number_manual_override', 'episode_number_verified_at',
        'video_variant_group_id')
    with Session(engine) as db:
        authority = {v.id: tuple(getattr(v, f) for f in fields) for v in db.scalars(select(Video))}
    engine.dispose()
    with (owner.root / known.target).open('ab') as f:
        f.write(b'changed in place')
    new = owner.root / 'Other Show/Other Show - 01.mkv'
    new.parent.mkdir()
    new.write_bytes(b'synthetic media')
    sibling = (owner.root / known.target).parent / 'Release - 04.mkv'
    sibling.write_bytes(b'synthetic media')
    result = scan(path, owner.root)
    assert (result.created, result.updated, result.removed) == (2, 1, 0)
    engine = make_engine(f'sqlite:///{path}')
    with Session(engine) as db:
        for action in manifest.payload.actions:
            if action.object_kind == 'video':
                v = db.get(Video, action.object_id)
                assert tuple(getattr(v, f) for f in fields) == authority[v.id]
                assert v.relative_path == action.target
                assert v.filename == Path(action.source).name
                assert v.audio_tracks[0].manual_language == 'cs'
                assert v.internal_subtitles[0].manual_language == 'sk'
    engine.dispose()
    stable = database_rows(path)
    assert scan(path, owner.root).unchanged == 5
    assert database_rows(path) == stable


@pytest.mark.parametrize('fault', ['missing', 'source_reappears', 'wrong_locator', 'replacement', 'broken_guard'])
def test_conflicts_after_release_stop_without_writes(small_case, fault):
    owner, path, manifest = small_case
    execute(small_case, resume_scanning=True)
    action = next(a for a in manifest.payload.actions if a.object_kind == 'video' and a.action == 'MOVE')
    target = owner.root / action.target
    if fault == 'missing':
        target.unlink()
    elif fault == 'replacement':
        replacement = target.with_suffix('.tmp')
        replacement.write_bytes(target.read_bytes())
        replacement.replace(target)
    elif fault == 'source_reappears':
        source = owner.root / action.source
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(target.read_bytes())
    elif fault == 'wrong_locator':
        import sqlite3
        with sqlite3.connect(path) as db:
            db.execute('UPDATE videos SET relative_path=? WHERE id=?', (action.source, action.object_id))
    else:
        path.with_name(path.name + '.execution-guard.json').write_text('{}')
    before, raw = database_rows(path), path.read_bytes()
    with pytest.raises(LibrarySafetyError):
        scan(path, owner.root, confirm_deletions=True)
    assert database_rows(path) == before and path.read_bytes() == raw


def test_partial_execution_cannot_release_or_delete_known_ids(small_case, monkeypatch):
    from app import target_execution_database as database
    owner, path, manifest = small_case
    original = database_rows(path)
    apply = database._ScratchSequenceDatabase._apply
    def interrupted(current, index):
        if manifest.payload.actions[index].action != 'KEEP':
            raise RuntimeError('interrupted between filesystem MOVE and DB locator update')
        return apply(current, index)
    monkeypatch.setattr(database._ScratchSequenceDatabase, '_apply', interrupted)
    with pytest.raises(Exception, match='interrupted'):
        execute(small_case, resume_scanning=True)
    assert database_rows(path) == original
    from app.target_execution_guard import complete_reorganization, ExecutionGuardStopped
    engine = make_engine(f'sqlite:///{path}')
    try:
        with pytest.raises(ExecutionGuardStopped):
            complete_reorganization(engine, owner.root)
    finally:
        engine.dispose()
    raw = path.read_bytes()
    for _ in range(2):
        with pytest.raises(LibrarySafetyError, match='[Rr]eorganization'):
            scan(path, owner.root, confirm_deletions=True)
        assert database_rows(path) == original and path.read_bytes() == raw


def test_torn_scanning_transition_remains_stop(small_case, monkeypatch):
    from app import target_execution_journal as journal
    owner, path, manifest = small_case
    write = journal._write_all
    def interrupted(fd, raw):
        if b'"state":"SCANNING"' in raw:
            write(fd, raw[:len(raw)//2])
            raise OSError('interrupted guard transition')
        return write(fd, raw)
    monkeypatch.setattr(journal, '_write_all', interrupted)
    with pytest.raises(Exception, match='interrupted guard transition'):
        execute(small_case, resume_scanning=True)
    before = database_rows(path)
    with pytest.raises(LibrarySafetyError):
        scan(path, owner.root, confirm_deletions=True)
    assert database_rows(path) == before


def test_scanning_transition_does_not_admit_startup(small_case):
    from app.target_execution_guard import ordinary_scan_admission
    from app.maintenance import MaintenanceStopped
    owner, path, manifest = small_case
    execute(small_case, resume_scanning=True)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with pytest.raises(MaintenanceStopped):
            with ordinary_scan_admission(engine, owner.root):
                pytest.fail('startup must not consume scanner routing permission')
    finally:
        engine.dispose()


@pytest.mark.parametrize('failure', ['fsync', 'close'])
def test_failed_scanning_transition_never_admits_scan(small_case, monkeypatch, failure):
    from app import target_execution_journal as journal
    import os
    import stat
    owner, path, manifest = small_case
    if failure == 'fsync':
        sync = os.fsync
        failed = False
        def broken(fd):
            nonlocal failed
            if (not failed and stat.S_ISREG(os.fstat(fd).st_mode)
                    and b'"state":"SCANNING"' in os.pread(fd, 1024*1024, 0)):
                failed = True
                raise OSError('injected transition fsync failure')
            return sync(fd)
        monkeypatch.setattr(os, 'fsync', broken)
    else:
        close = journal.DurableJournal.close
        def broken(writer):
            close(writer)
            raise OSError('injected completion close failure')
        monkeypatch.setattr(journal.DurableJournal, 'close', broken)
    with pytest.raises(Exception, match='(fsync|cleanup failed)'):
        execute(small_case, resume_scanning=True)
    before = database_rows(path)
    with pytest.raises(LibrarySafetyError):
        scan(path, owner.root, confirm_deletions=True)
    assert database_rows(path) == before


@pytest.mark.parametrize('fault', ['video_inventory', 'subtitle_inventory', 'before_commit'])
def test_evidence_lost_during_scan_rolls_back_all_changes(small_case, monkeypatch, fault):
    from app.scanner import service
    owner, path, manifest = small_case
    execute(small_case, resume_scanning=True)
    action = next(a for a in manifest.payload.actions if a.object_kind == 'video' and a.action == 'MOVE')
    if fault == 'video_inventory':
        walk = service.iter_videos
        monkeypatch.setattr(service, 'iter_videos', lambda root:
            (p for p in walk(root) if p != root / action.target))
    elif fault == 'subtitle_inventory':
        monkeypatch.setattr(service, 'iter_external_subtitles', lambda root: iter(()))
    else:
        sync = service._sync_external_subtitles
        def lost(*a, **k):
            sync(*a, **k)
            (owner.root / action.target).unlink()
        monkeypatch.setattr(service, '_sync_external_subtitles', lost)
    before, raw = database_rows(path), path.read_bytes()
    with pytest.raises(LibrarySafetyError):
        scan(path, owner.root, confirm_deletions=True)
    assert database_rows(path) == before and path.read_bytes() == raw


@pytest.mark.parametrize('normal_operation', [False, True])
def test_new_episode_preserves_reorganized_automatic_part_hierarchy(tmp_path, monkeypatch, normal_operation):
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE)
    fields = ('id', 'part_type', 'part_number', 'season_number', 'sort_order', 'original_folder_name')
    with ScratchManifestExecution.create(tmp_path) as owner:
        source_folder = owner.root / 'Old Show/Season 1 Part 2'
        source_folder.mkdir(parents=True, mode=0o700)
        source_folder.parent.chmod(0o700)
        for number in range(1, 4):
            source = source_folder / f'Release - {number:02}.mkv'
            source.write_bytes(b'synthetic media')
            source.chmod(0o600)
        path = owner.database_path
        engine = make_engine(f'sqlite:///{path}')
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as db:
                assert scan_library(db, owner.root).created == 3
                videos = db.scalars(select(Video).order_by(Video.id)).all()
                original_ids = {v.id for v in videos}
                title = videos[0].catalog_title
                assert not title.hierarchy_manual_override
                assert (title.part_type, title.part_number, title.season_number, title.sort_order) == (
                    'part', 2, 1, 1002)
                hierarchy = tuple(getattr(title, field) for field in fields)
                title_id, collection_id = title.id, title.catalog_collection_id
                attach_metadata(title, str(title.id), 'Show')
                confirm_physical_naming_choice(db, title.collection, 'New Show', 'custom', now=NOW)
                confirm_physical_naming_choice(db, title, 'Custom', 'custom', now=NOW)
                db.commit()
            manifest = approve(generate(owner, path))
            assert not manifest.payload.generation_diagnostics
            assert any(p.object_kind == 'title' and p.object_id == title_id
                for p in manifest.payload.locator_patches)
            execute((owner, path, manifest), resume_scanning=True)
            if normal_operation:
                from app.target_execution_guard import complete_reorganization
                complete_reorganization(engine, owner.root)
            assert scan(path, owner.root).unchanged == 3
            target = next(a.target for a in manifest.payload.actions if a.object_kind == 'video')
            assert Path(target).parent.as_posix() == 'New Show/Season 01'
            new = (owner.root / target).parent / 'Release - 04.mkv'
            new.write_bytes(b'synthetic media')
            # Also verify that manifest preservation cannot suppress initialization
            # of a genuinely new title in a different collection.
            unrelated = owner.root / 'Other Show/Season 2/Release - 01.mkv'
            unrelated.parent.mkdir(parents=True)
            unrelated.write_bytes(b'synthetic media')
            result = scan(path, owner.root)
            assert (result.created, result.removed, result.errors) == (2, 0, 0)
            with Session(engine) as db:
                videos = db.scalars(select(Video)).all()
                assert original_ids < {v.id for v in videos}
                assert {v.catalog_title_id for v in videos if v.id in original_ids} == {title_id}
                added = next(v for v in videos if v.relative_path == new.relative_to(owner.root).as_posix())
                assert added.id not in original_ids
                assert (added.catalog_title_id, added.catalog_collection_id) == (title_id, collection_id)
                title = added.catalog_title
                assert not title.hierarchy_manual_override
                assert tuple(getattr(title, field) for field in fields) == hierarchy
                other = next(v for v in videos if v.relative_path == unrelated.relative_to(owner.root).as_posix())
                assert other.catalog_title_id != title_id
                assert (other.catalog_title.part_type, other.catalog_title.season_number,
                    other.catalog_title.part_number, other.catalog_title.original_folder_name) == (
                        'season', 2, None, 'Season 2')
            stable = database_rows(path)
            assert scan(path, owner.root).unchanged == 5
            assert database_rows(path) == stable
        finally:
            engine.dispose()
