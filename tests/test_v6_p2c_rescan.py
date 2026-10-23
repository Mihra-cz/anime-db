"""P2C: real sealed MOVE + locator-only SQLite patch + scanner rescan."""
from dataclasses import replace
import sqlite3

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.database import make_engine
from app.models import AudioTrack, InternalSubtitle, Video, CatalogTitle
from app.scanner import scan_library, LibrarySafetyError
from app.target_execution_manifest import manifest_from_json, manifest_to_json, WarningAcknowledgement
from app.target_execution_sequence import ScratchManifestExecution
from test_physical_naming import title, attach_metadata
from test_v6_execution_file_actions import prepare_mixed, generate
from test_v6_execution_projection import TABLES, expected_rows
from v6_database_case import database_rows


def approve(manifest):
    return replace(manifest, acknowledgements=tuple(WarningAcknowledgement(
        w.warning_id, w.warning_class, w.object_kind, w.object_id,
        'synthetic operator', '2026-10-23T10:00:00Z') for w in manifest.payload.warnings))


def apply_approved_post_state(root, path, manifest):
    """Simulate only the exact approved moves and patches; no identity heuristics."""
    original = database_rows(path)
    for action in manifest.payload.actions:
        if action.action != 'KEEP':
            source, target = root / action.source, root / action.target
            target.parent.mkdir(parents=True, exist_ok=True)
            assert not target.exists()
            source.rename(target)
    with sqlite3.connect(path) as db:
        for patch in manifest.payload.locator_patches:
            table = TABLES[patch.object_kind].name
            changed = db.execute(
                f'UPDATE "{table}" SET "{patch.field}"=? WHERE id=? AND "{patch.field}"=?',
                (patch.after, patch.object_id, patch.before))
            assert changed.rowcount == 1
    assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)


@pytest.fixture
def case(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        _, path = prepare_mixed(owner)
        engine = make_engine(f'sqlite:///{path}')
        with Session(engine) as db:
            db.add_all([
                AudioTrack(video_id=100, stream_index=1, codec='aac', language='jpn', manual_language='cs'),
                InternalSubtitle(video_id=100, stream_index=2, codec='ass', language='eng', manual_language='sk'),
            ])
            db.commit()
        engine.dispose()
        manifest = approve(generate(owner, path))
        yield owner, path, manifest


def rescan(path, root, manifest):
    engine = make_engine(f'sqlite:///{path}')
    statements = []
    event.listen(engine, 'before_cursor_execute',
        lambda conn, cursor, sql, params, context, many: statements.append(sql))
    try:
        with Session(engine) as db:
            result = scan_library(db, root, execution_manifest=manifest)
            assert not db.new and not db.dirty and not db.deleted
        assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA')) for s in statements)
        return result
    finally:
        engine.dispose()


def test_move_patch_rescan_preserves_all_rows_and_repeat_is_idempotent(case):
    owner, path, manifest = case
    original = database_rows(path)
    immutable = manifest_to_json(manifest)
    apply_approved_post_state(owner.root, path, manifest)
    before = database_rows(path)
    for _ in range(2):
        result = rescan(path, owner.root, manifest_from_json(immutable))
        assert (result.found, result.unchanged, result.created, result.updated, result.removed, result.errors) == (3, 3, 0, 0, 0, 0)
        assert database_rows(path) == before
    assert before == expected_rows(original, manifest.payload.locator_patches)
    assert manifest_to_json(manifest) == immutable


def test_shared_season_keeps_distinct_owner_ids(case):
    owner, path, _ = case
    engine = make_engine(f'sqlite:///{path}')
    with Session(engine) as db:
        first = db.get(Video, 100).catalog_title
        first.part_type = first.part_type_manual = 'part'
        first.part_number = first.part_number_manual = 1
        second = title(db, first.collection, name='Part 2', season=1, part=2, kind='part', metadata=False)
        attach_metadata(second, str(second.id), 'Show')
        second.relative_root_path = first.relative_root_path
        db.get(Video, 200).catalog_title = second
        db.commit()
    engine.dispose()
    manifest = approve(generate(owner, path))
    assert not manifest.payload.generation_diagnostics
    apply_approved_post_state(owner.root, path, manifest)
    before = database_rows(path)
    assert rescan(path, owner.root, manifest).unchanged == 3
    assert database_rows(path) == before
    engine = make_engine(f'sqlite:///{path}')
    with Session(engine) as db:
        owners = db.scalars(select(CatalogTitle)).all()
        assert len(owners) == 2 and owners[0].id != owners[1].id
        assert owners[0].relative_root_path == owners[1].relative_root_path == 'Show/Season 01'
    engine.dispose()


@pytest.mark.parametrize('fault', ['missing_video', 'new_video', 'stat', 'missing_subtitle',
    'partial_patch', 'wrong_id', 'missing_carried', 'bad_seal', 'foreign_root', 'duplicate_identity', 'unapproved', 'omitted_patch'])
def test_unsafe_evidence_stops_before_any_write(case, fault):
    owner, path, manifest = case
    apply_approved_post_state(owner.root, path, manifest)
    scan_root = owner.root
    action = next(a for a in manifest.payload.actions if a.object_kind == 'video' and a.object_id == 100)
    if fault == 'missing_video':
        (owner.root / action.target).unlink()
    elif fault == 'new_video':
        (owner.root / 'Show/new.mkv').write_bytes(b'new')
    elif fault == 'stat':
        (owner.root / action.target).write_bytes(b'changed bytes')
    elif fault == 'missing_subtitle':
        subtitle = next(a for a in manifest.payload.actions if a.object_kind == 'subtitle')
        (owner.root / subtitle.target).unlink()
    elif fault == 'partial_patch':
        with sqlite3.connect(path) as db:
            db.execute('UPDATE videos SET relative_path=? WHERE id=100', (action.source,))
    elif fault == 'wrong_id':
        with sqlite3.connect(path) as db:
            db.execute('UPDATE videos SET id=9000 WHERE id=100')
    elif fault == 'missing_carried':
        from test_v6_execution_filesystem import seal
        manifest = seal(manifest, duplicate_evidence=())
    elif fault == 'omitted_patch':
        from test_v6_execution_filesystem import seal
        manifest = seal(manifest, locator_patches=tuple(p for p in manifest.payload.locator_patches
            if not (p.object_kind == 'video' and p.object_id == 100 and p.field == 'relative_path')))
    elif fault == 'unapproved':
        manifest = replace(manifest, acknowledgements=())
    elif fault == 'bad_seal':
        manifest = replace(manifest, manifest_id='bad')
    elif fault == 'foreign_root':
        scan_root = owner.root / 'Show'
    else:
        from test_v6_execution_filesystem import seal, digest
        other = replace(action, source='other/source.mkv', action_id=digest((
            action.object_kind, action.object_id, action.action, 'other/source.mkv', action.target)))
        manifest = seal(manifest, manifest.payload.actions + (other,))
        manifest_to_json(manifest)  # Valid seal: ambiguity itself must be refused.
    before, content = database_rows(path), path.read_bytes()
    with pytest.raises(LibrarySafetyError):
        rescan(path, scan_root, manifest)
    assert database_rows(path) == before and path.read_bytes() == content


@pytest.mark.parametrize('confirm_deletions', [False, True])
def test_reorganized_row_without_manifest_stops_after_consistent_ordinary_prestate(tmp_path, monkeypatch, confirm_deletions):
    from app.database import Base
    from app.hierarchy_review import set_manual_title_hierarchy
    from test_scanner import PROBE_RESULT
    from test_physical_naming import NOW
    # Only the technical media probe is substituted; scanner/parser/hierarchy
    # and manifest generation run normally against real SQLite and files.
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE_RESULT)
    with ScratchManifestExecution.create(tmp_path) as owner:
        source = owner.root / 'Show/Season 1/Release - 01.mkv'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'synthetic video')
        path = tmp_path / 'consistent.sqlite'
        engine = make_engine(f'sqlite:///{path}')
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as db:
                assert scan_library(db, owner.root).created == 1
                video = db.scalar(select(Video))
                assert video.filename == source.name == source.relative_to(owner.root).name
                assert video.relative_path == source.relative_to(owner.root).as_posix()
                assert (video.size, video.mtime_ns) == (source.stat().st_size, source.stat().st_mtime_ns)
                set_manual_title_hierarchy(video.catalog_title, season_number=1,
                    season_label=None, part_type='season', sort_order=1, hierarchy_verified=True)
                attach_metadata(video.catalog_title, str(video.catalog_title_id), 'Show')
                video.content_type_manual = 'episode'
                video.episode_number_manual_override = 1
                video.episode_number_verified_at = NOW
                video.manual_hardsub_cs = True
                video.manual_hardsub_verified_at = NOW
                db.commit()
                assert scan_library(db, owner.root).unchanged == 1
            original = database_rows(path)
            manifest = approve(generate(owner, path))
            assert not manifest.payload.generation_diagnostics
            assert any(a.action == 'MOVE' and a.object_kind == 'video' for a in manifest.payload.actions)
            apply_approved_post_state(owner.root, path, manifest)
            expected = expected_rows(original, manifest.payload.locator_patches)
            with Session(engine) as db, pytest.raises(LibrarySafetyError, match='execution'):
                scan_library(db, owner.root, confirm_deletions=confirm_deletions)
            assert database_rows(path) == expected  # Fresh independent SQLite connection.
        finally:
            engine.dispose()


@pytest.mark.parametrize('status', ['unresolved', 'confirmed_no_match'])
def test_subtitle_refresh_preserves_historical_source_filename(tmp_path, status):
    from app.database import Base
    from app.models import UnresolvedExternalSubtitle
    from app.scanner.service import _sync_external_subtitles
    root = tmp_path / 'media'
    root.mkdir()
    (root / 'current.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\nHello\n')
    engine = make_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        sub = UnresolvedExternalSubtitle(relative_path='current.srt', filename='Original NCED02.srt',
            extension='.srt', status=status)
        db.add(sub)
        db.commit()
        identity = sub.id
        _sync_external_subtitles(db, root, [])
        db.commit()
        assert (sub.id, sub.filename, sub.relative_path, sub.status) == (
            identity, 'Original NCED02.srt', 'current.srt', status)
    engine.dispose()


def test_stale_session_cache_cannot_mask_unapplied_locator_patch(case):
    owner, path, manifest = case
    apply_approved_post_state(owner.root, path, manifest)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine, expire_on_commit=False) as db:
            cached = db.get(Video, 100)
            target = cached.relative_path
            db.commit()  # Retain clean cached rows across the external locator change.
            action = next(a for a in manifest.payload.actions if a.object_kind == 'video' and a.object_id == 100)
            with sqlite3.connect(path) as connection:
                connection.execute('UPDATE videos SET relative_path=? WHERE id=100', (action.source,))
            assert cached.relative_path == target
            before = database_rows(path)
            with pytest.raises(LibrarySafetyError, match='locator'):
                scan_library(db, owner.root, execution_manifest=manifest)
            assert database_rows(path) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize('operation', ['core', 'flushed', 'dirty', 'select'])
@pytest.mark.parametrize('entrypoint', ['scanner', 'verifier'])
def test_open_transaction_stops_without_ending_callers_transaction(case, operation, entrypoint):
    from sqlalchemy import update
    from app.scanner.execution_rescan import verify_execution_rescan
    owner, path, manifest = case
    apply_approved_post_state(owner.root, path, manifest)
    if operation != 'select':
        with sqlite3.connect(path) as connection:
            for patch in manifest.payload.locator_patches:
                connection.execute(f'UPDATE "{TABLES[patch.object_kind].name}" SET "{patch.field}"=? WHERE id=?',
                    (patch.before, patch.object_id))
    committed_before = database_rows(path)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as db:
            if operation == 'core':
                for patch in manifest.payload.locator_patches:
                    table = TABLES[patch.object_kind]
                    db.execute(update(table).where(table.c.id == patch.object_id).values({patch.field: patch.after}))
            elif operation in {'dirty', 'flushed'}:
                models = {model.__table__.name: model for model in (Video, CatalogTitle)}
                from app.models import CatalogCollection, ExternalSubtitle, UnresolvedExternalSubtitle
                models.update({m.__table__.name: m for m in (CatalogCollection, ExternalSubtitle, UnresolvedExternalSubtitle)})
                for patch in manifest.payload.locator_patches:
                    with db.no_autoflush:
                        row = db.get(models[TABLES[patch.object_kind].name], patch.object_id)
                        setattr(row, patch.field, patch.after)
                if operation == 'flushed':
                    db.flush()
            else:
                db.scalar(select(Video.id))  # SELECT autobegin is deliberately rejected too.
            assert db.in_transaction()
            if operation != 'dirty':
                assert not db.new and not db.dirty and not db.deleted
            assert database_rows(path) == committed_before  # Independent connection cannot see pending patches.
            statements = []
            event.listen(engine, 'before_cursor_execute',
                lambda conn, cursor, sql, params, context, many: statements.append(sql))
            transaction = db.get_transaction()
            error = LibrarySafetyError if entrypoint == 'scanner' else ValueError
            with pytest.raises(error, match='transaction'):
                if entrypoint == 'scanner':
                    scan_library(db, owner.root, execution_manifest=manifest)
                else:
                    verify_execution_rescan(db, owner.root, manifest)
            assert db.get_transaction() is transaction and transaction.is_active
            assert not statements  # Refusal happens before SELECT, flush, commit or rollback.
            assert database_rows(path) == committed_before
            db.rollback()  # Only the caller ends its transaction; apparent post-state disappears.
        assert database_rows(path) == committed_before
    finally:
        engine.dispose()


@pytest.mark.parametrize('kind,field', [('title', 'relative_root_path'),
    ('collection', 'relative_root_path'), ('video', 'root_folder')])
def test_unapplied_non_file_locator_patch_stops_at_explicit_gate(case, kind, field):
    owner, path, manifest = case
    apply_approved_post_state(owner.root, path, manifest)
    patch = next(p for p in manifest.payload.locator_patches if p.object_kind == kind and p.field == field)
    with sqlite3.connect(path) as db:
        db.execute(f'UPDATE "{TABLES[kind].name}" SET "{field}"=? WHERE id=?', (patch.before, patch.object_id))
    before = database_rows(path)
    with pytest.raises(LibrarySafetyError, match='locator patch missing or stale'):
        rescan(path, owner.root, manifest)
    assert database_rows(path) == before


def test_keep_with_file_locator_patch_stops_at_explicit_gate(case):
    from app.target_execution_manifest import LocatorPatch
    from test_v6_execution_filesystem import seal
    owner, path, manifest = case
    apply_approved_post_state(owner.root, path, manifest)
    keep = next(a for a in manifest.payload.actions if a.object_kind == 'video' and a.action == 'KEEP')
    patch = LocatorPatch('video', keep.object_id, 'relative_path', 'old/fictitious.mkv', keep.target)
    manifest = seal(manifest, locator_patches=(*manifest.payload.locator_patches, patch))
    manifest_to_json(manifest)
    before = database_rows(path)
    with pytest.raises(LibrarySafetyError, match='KEEP cannot change a file locator'):
        rescan(path, owner.root, manifest)
    assert database_rows(path) == before


def test_ordinary_unresolved_subtitle_guard_independent_of_video_guard(tmp_path):
    from app.database import Base
    from app.models import UnresolvedExternalSubtitle
    root = tmp_path / 'library'
    root.mkdir()
    (root / 'renamed.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\nHello\n')
    path = tmp_path / 'library.sqlite'
    engine = make_engine(f'sqlite:///{path}')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(UnresolvedExternalSubtitle(relative_path='renamed.srt', filename='original.srt', extension='.srt'))
            db.commit()
        before = database_rows(path)
        with Session(engine) as db, pytest.raises(LibrarySafetyError, match='Subtitle locator'):
            scan_library(db, root, confirm_deletions=True)
        assert database_rows(path) == before
    finally:
        engine.dispose()


def test_fresh_readonly_admission_uses_committed_projection(case):
    from sqlalchemy import create_engine
    owner, path, manifest = case
    apply_approved_post_state(owner.root, path, manifest)
    before = database_rows(path)
    engine = create_engine('sqlite://', creator=lambda: sqlite3.connect(f'file:{path}?mode=ro', uri=True))
    try:
        with Session(engine) as db:
            assert not db.in_transaction()
            assert scan_library(db, owner.root, execution_manifest=manifest).unchanged == 3
            assert not db.in_transaction()
        assert database_rows(path) == before
    finally:
        engine.dispose()


def test_external_connection_transaction_is_not_adopted_or_ended(case):
    from sqlalchemy import update
    owner, path, manifest = case
    apply_approved_post_state(owner.root, path, manifest)
    before = database_rows(path)
    engine = make_engine(f'sqlite:///{path}')
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(update(Video).where(Video.id == 100).values(root_folder='pending'))
            with Session(bind=connection) as db:
                assert not db.in_transaction()
                with pytest.raises(LibrarySafetyError):
                    scan_library(db, owner.root, execution_manifest=manifest)
                assert not db.in_transaction() and transaction.is_active
                assert database_rows(path) == before
            assert transaction.is_active
            transaction.rollback()
        assert database_rows(path) == before
    finally:
        engine.dispose()
