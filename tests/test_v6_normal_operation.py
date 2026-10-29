"""Completed scratch execution can retire into the existing web/scanner lifecycle."""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base, make_engine
from app.main import create_app
from app.maintenance import ScratchMaintenance, MaintenanceBusy
from app.models import Video, ExternalSubtitle, ExternalSubtitleCompatibility, UnresolvedExternalSubtitle
from app.scanner import LibrarySafetyError
from app import target_execution_guard as guard
from test_scanner_reorganization import case, execute, scan
from test_v6_p2c_execution_guard import small_case, snapshot
from v6_database_case import database_rows


def complete(path, root):
    engine = make_engine(f'sqlite:///{path}')
    try:
        guard.complete_reorganization(engine, root)
    finally:
        engine.dispose()


def settings_for(path, root):
    return Settings(anime_path=root, database_url=f'sqlite:///{path}',
        metadata_enabled=False, metadata_download_artwork=False,
        metadata_artwork_directory=path.parent / 'normal-artwork')


def authority(path):
    fields = ('id', 'filename', 'file_type', 'catalog_title_id', 'catalog_collection_id',
        'content_type_manual', 'episode_number_manual_override',
        'episode_number_verified_at', 'video_variant_group_id')
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as db:
            return {v.id: (tuple(getattr(v, f) for f in fields),
                v.audio_tracks[0].manual_language, v.internal_subtitles[0].manual_language)
                for v in db.scalars(select(Video))}
    finally:
        engine.dispose()


def test_unstarted_execution_cannot_grant_normal_operation(small_case):
    owner, path, _ = small_case
    before = database_rows(path)
    with pytest.raises(guard.ExecutionGuardStopped):
        complete(path, owner.root)
    assert database_rows(path) == before


def test_known_subtitle_locator_preserves_identity_and_compatibility(tmp_path):
    from app.scanner.service import _sync_external_subtitles
    root = tmp_path / 'library'
    (root / 'Subs').mkdir(parents=True)
    (root / 'Subs/known.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nTest\n')
    engine = make_engine('sqlite://')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            video = Video(relative_path='Show/Episode.mkv', root_folder='Show',
                filename='Historical - 01.mkv', size=1, mtime_ns=1)
            subtitle = ExternalSubtitle(relative_path='Subs/known.srt', codec='srt', manual_language='sk')
            db.add_all([video, subtitle])
            db.flush()
            db.add(ExternalSubtitleCompatibility(external_subtitle=subtitle, video=video,
                status='automatic_match', match_method='filename'))
            db.commit()
            ident, video_id = subtitle.id, video.id
            _sync_external_subtitles(db, root, [video], preserve_known=True)
            db.commit()
            db.expire_all()
            kept = db.get(ExternalSubtitle, ident)
            assert kept is not None and kept.manual_language == 'sk'
            assert [(p.video_id, p.status) for p in kept.compatibilities] == [(video_id, 'automatic_match')]
    finally:
        engine.dispose()


def test_normal_subtitle_becomes_unresolved_and_relinks_after_video_returns(case):
    from app.external_subtitle_compatibility import confirm_compatible
    from app.subtitle_review import rejected_video_ids
    from test_physical_naming import NOW
    owner, path, manifest = case
    execute(case, resume_scanning=True)
    complete(path, owner.root)
    actions = [a for a in manifest.payload.actions if a.object_kind == 'video']
    removed, surviving = actions[:2]
    target = owner.root / removed.target
    content = target.read_bytes()
    original_authority = authority(path)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as db:
            removed_subtitle = db.scalar(select(ExternalSubtitle).join(
                ExternalSubtitleCompatibility).where(
                    ExternalSubtitleCompatibility.video_id == removed.object_id))
            subtitle_path = removed_subtitle.relative_path
            # This asset stays automatic; surviving assets retain human authority.
            removed_subtitle.manual_language = None
            removed_subtitle.rejected_video_ids_json = f'[{surviving.object_id}]'
            surviving_subtitle = db.scalar(select(ExternalSubtitle).join(
                ExternalSubtitleCompatibility).where(
                    ExternalSubtitleCompatibility.video_id == surviving.object_id))
            decision = confirm_compatible(db, surviving_subtitle,
                db.get(Video, surviving.object_id), note='Keep this manual decision', verified_at=NOW)
            db.commit()
            manual_ids = surviving_subtitle.id, decision.id
            manual_state = (surviving_subtitle.manual_language, decision.status,
                decision.match_method, decision.verified_at, decision.note)
        target.unlink()
        assert scan(path, owner.root, confirm_deletions=True).removed == 1
        with Session(engine) as db:
            assert db.scalar(select(ExternalSubtitle).where(
                ExternalSubtitle.relative_path == subtitle_path)) is None
            unresolved = db.scalar(select(UnresolvedExternalSubtitle).where(
                UnresolvedExternalSubtitle.relative_path == subtitle_path))
            assert unresolved is not None and unresolved.status == 'unresolved'
            assert rejected_video_ids(unresolved) == {surviving.object_id}
        target.write_bytes(content)
        assert scan(path, owner.root).created == 1
        with Session(engine) as db:
            returned = db.scalar(select(Video).where(Video.relative_path == removed.target))
            linked = db.scalar(select(ExternalSubtitle).where(
                ExternalSubtitle.relative_path == subtitle_path))
            assert linked is not None and linked.match_method == 'automatic'
            assert [(p.video_id, p.status) for p in linked.compatibilities] == [(returned.id, 'automatic_match')]
            assert rejected_video_ids(linked) == {surviving.object_id}
            assert db.scalar(select(UnresolvedExternalSubtitle).where(
                UnresolvedExternalSubtitle.relative_path == subtitle_path)) is None
            kept = db.get(ExternalSubtitle, manual_ids[0])
            decision = db.get(ExternalSubtitleCompatibility, manual_ids[1])
            assert (kept.manual_language, decision.status, decision.match_method,
                decision.verified_at, decision.note) == manual_state
        remaining = authority(path)
        for ident, expected in original_authority.items():
            if ident != removed.object_id:
                assert remaining[ident] == expected
    finally:
        engine.dispose()


def test_reorganized_library_restarts_web_and_supports_normal_mutations(case):
    owner, path, manifest = case
    original_authority = authority(path)
    execute(case, resume_scanning=True)
    history = {p: p.read_bytes() for p in
        (owner.journal_path, owner.directory_evidence_path, *owner.evidence_paths)}
    prefix = guard._guard_path(path).read_bytes()
    rows = database_rows(path)
    complete(path, owner.root)
    assert database_rows(path) == rows
    assert guard._guard_path(path).read_bytes().startswith(prefix)
    complete(path, owner.root)  # Idempotent completion does not append again.
    owner.close()
    root = owner.root
    # New app/engine and real lifespan, after the original executor closed.
    web = create_app(settings_for(path, root))
    with TestClient(web) as client:
        startup = database_rows(path)
        for route in ('/health', '/', '/hierarchy-review', '/media-check'):
            assert client.get(route).status_code == 200
        assert database_rows(path) == startup
        with web.state.sessions() as db:
            subtitle_authority = {s.id: s.manual_language for s in db.scalars(select(ExternalSubtitle))}
        compatibility = startup['external_subtitle_compatibilities']
        response = client.post('/scan', follow_redirects=False)
        assert response.status_code == 303 and 'error=' not in response.headers['location']
        with web.state.sessions() as db:
            for ident, language in subtitle_authority.items():
                subtitle = db.get(ExternalSubtitle, ident)
                assert subtitle is not None, 'canonical Subs locator lost its known subtitle ID'
                assert subtitle.manual_language == language
        assert database_rows(path)['external_subtitle_compatibilities'] == compatibility
        assert scan(path, root).unchanged == 3
        actions = [a for a in manifest.payload.actions if a.object_kind == 'video']
        added = (root / actions[0].target).parent / 'Release - 04.mkv'
        added.write_bytes(b'new episode')
        added.with_suffix('.cs.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nAhoj!\n')
        assert scan(path, root).created == 1
        # Both an in-place change and a new inode at the known path keep its ID.
        target = root / actions[0].target
        with target.open('ab') as output:
            output.write(b'updated video')
        assert scan(path, root).updated == 1
        replacement = target.with_suffix('.replacement')
        replacement.write_bytes(b'new copy of this episode')
        replacement.replace(target)
        assert scan(path, root).updated == 1
        removed = actions[-1]
        (root / removed.target).unlink()
        # 1/4 exceeds the existing 20% deletion gate; web confirmation is required.
        before = database_rows(path)
        response = client.post('/scan', follow_redirects=False)
        assert 'confirm_deletions=true' in response.headers['location']
        assert database_rows(path) == before
        response = client.post('/scan', data={'confirm_deletions': 'true'}, follow_redirects=False)
        assert response.status_code == 303 and 'error=' not in response.headers['location']
        remaining = authority(path)
        assert removed.object_id not in remaining
        for ident, values in original_authority.items():
            if ident != removed.object_id:
                assert remaining[ident] == values
        engine = make_engine(f'sqlite:///{path}')
        try:
            with Session(engine) as db:
                new = db.scalar(select(Video).where(Video.relative_path == added.relative_to(root).as_posix()))
                assert new.id not in original_authority
                assert new.catalog_title_id == original_authority[actions[0].object_id][0][3]
                assert db.scalar(select(ExternalSubtitle).where(
                    ExternalSubtitle.relative_path == added.with_suffix('.cs.srt').relative_to(root).as_posix()))
        finally:
            engine.dispose()
        stable = database_rows(path)
        assert scan(path, root).unchanged == 3
        assert database_rows(path) == stable
    assert all(p.read_bytes() == content for p, content in history.items())
    # A second entirely new application still starts after deletion/replacement.
    with TestClient(create_app(settings_for(path, root))) as client:
        assert client.get('/health').json() == {'status': 'ok'}


@pytest.mark.parametrize('fault', ['no_transition', 'journal', 'identity', 'guard',
    'locator', 'source_conflict', 'missing', 'replacement'])
def test_unverified_completion_never_enables_startup(small_case, fault):
    owner, path, manifest = small_case
    execute(small_case, resume_scanning=fault != 'no_transition')
    action = next(a for a in manifest.payload.actions if a.action == 'MOVE' and a.object_kind == 'video')
    target = owner.root / action.target
    if fault == 'journal':
        owner.journal_path.write_bytes(owner.journal_path.read_bytes()[:-8])
    elif fault == 'identity':
        owner.evidence_paths[0].write_bytes(b'corrupt identity evidence')
    elif fault == 'guard':
        guard._guard_path(path).write_bytes(b'{"state":"NORMAL"}\n')
    elif fault == 'locator':
        with sqlite3.connect(path) as db:
            db.execute('UPDATE videos SET relative_path=? WHERE id=?', (action.source, action.object_id))
    elif fault == 'source_conflict':
        source = owner.root / action.source
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(target.read_bytes())
    elif fault == 'missing':
        target.unlink()
    elif fault == 'replacement':
        other = target.with_suffix('.tmp')
        other.write_bytes(target.read_bytes())
        other.replace(target)
    before, raw, files = database_rows(path), path.read_bytes(), snapshot(owner.root)
    marker = guard._guard_path(path).read_bytes()
    with pytest.raises(guard.ExecutionGuardStopped) as caught:
        complete(path, owner.root)
    if fault == 'locator':
        assert 'completion rescan differs: execution locator patch missing or stale' in str(caught.value)
        assert isinstance(caught.value.__cause__, ValueError)
        assert guard._guard_path(path).read_bytes() == marker
    with pytest.raises(guard.ExecutionGuardStopped):
        create_app(settings_for(path, owner.root))
    assert database_rows(path) == before and path.read_bytes() == raw
    assert snapshot(owner.root) == files


def test_exclusive_maintenance_is_required_for_completion(small_case):
    owner, path, _ = small_case
    execute(small_case, resume_scanning=True)
    with ScratchMaintenance(path, owner.root).writer(), pytest.raises(MaintenanceBusy):
        complete(path, owner.root)


def test_normal_startup_refuses_legacy_path_reconstruction(small_case):
    owner, path, _ = small_case
    execute(small_case, resume_scanning=True)
    complete(path, owner.root)
    # This low-level fixture deliberately has user_version=0. Its current schema
    # does not authorize replaying legacy hierarchy inference on canonical paths.
    before, raw = database_rows(path), path.read_bytes()
    with pytest.raises(guard.ExecutionGuardStopped):
        with TestClient(create_app(settings_for(path, owner.root))):
            pytest.fail('unversioned canonical database was reconstructed at startup')
    assert database_rows(path) == before and path.read_bytes() == raw


@pytest.mark.parametrize('damage', ['journal', 'identity', 'guard'])
def test_normal_operation_still_requires_intact_completion_history(small_case, damage):
    owner, path, _ = small_case
    execute(small_case, resume_scanning=True)
    complete(path, owner.root)
    artifact = {'journal': owner.journal_path, 'identity': owner.evidence_paths[0],
        'guard': guard._guard_path(path)}[damage]
    artifact.write_bytes(artifact.read_bytes()[:-8])
    before, raw = database_rows(path), path.read_bytes()
    with pytest.raises(guard.ExecutionGuardStopped):
        create_app(settings_for(path, owner.root))
    with pytest.raises(LibrarySafetyError):
        scan(path, owner.root, confirm_deletions=True)
    assert database_rows(path) == before and path.read_bytes() == raw


def test_lost_normal_admission_during_scan_rolls_back(small_case, monkeypatch):
    from app.scanner import service
    owner, path, _ = small_case
    execute(small_case, resume_scanning=True)
    complete(path, owner.root)
    sync = service._sync_external_subtitles
    def reverted(*args, **kwargs):
        sync(*args, **kwargs)
        marker = guard._guard_path(path)
        marker.write_bytes(b''.join(marker.read_bytes().splitlines(keepends=True)[:2]))
    monkeypatch.setattr(service, '_sync_external_subtitles', reverted)
    before, raw = database_rows(path), path.read_bytes()
    with pytest.raises(LibrarySafetyError):
        scan(path, owner.root)
    assert database_rows(path) == before and path.read_bytes() == raw


@pytest.mark.parametrize('failure', ['partial_write', 'fsync'])
def test_failed_normal_transition_remains_blocked(small_case, monkeypatch, failure):
    from app import target_execution_journal as journal
    import os
    owner, path, _ = small_case
    execute(small_case, resume_scanning=True)
    write, sync = journal._write_all, os.fsync
    def interrupted(fd, raw):
        if b'"state":"NORMAL"' in raw and failure == 'partial_write':
            write(fd, raw[:len(raw)//2])
            raise OSError('completion append interrupted')
        return write(fd, raw)
    def failed_sync(fd):
        if failure == 'fsync' and b'"state":"NORMAL"' in os.pread(fd, 1024*1024, 0):
            raise OSError('completion durability unconfirmed')
        return sync(fd)
    with monkeypatch.context() as fault:
        fault.setattr(journal, '_write_all', interrupted)
        fault.setattr(os, 'fsync', failed_sync)
        with pytest.raises(guard.ExecutionGuardStopped):
            complete(path, owner.root)
    with pytest.raises(LibrarySafetyError):
        scan(path, owner.root, confirm_deletions=True)
    with pytest.raises(guard.ExecutionGuardStopped):
        create_app(settings_for(path, owner.root))
