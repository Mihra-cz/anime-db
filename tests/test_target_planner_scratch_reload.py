"""Real locator-only SQLite reloads through the normal SELECT-only planner loader."""
from dataclasses import replace
from dataclasses import asdict
import json
import sqlite3
from pathlib import PurePosixPath

import pytest
from sqlalchemy import event

from app.database import Base
from app.models import Video, UnresolvedExternalSubtitle
from app.target_planner import project_container_locators
from app.target_planner_filesystem import plan_library
from app.target_planner_service import load_planner_context, readonly_planner_session
from app.target_planner_types import DuplicateExecutionEvidence, SideAssetEvidence, FilesystemEntry, FilesystemSnapshot
from test_physical_naming import session, collection, title, attach_metadata


def accounting(row):
    assert hasattr(row.duplicate, 'side_asset_accounting'), 'Explicit tri-state accounting missing'
    return row.duplicate.side_asset_accounting


def domain_rows(path):
    allowed = {'videos': {'relative_path', 'root_folder'},
               'catalog_collections': {'relative_root_path'},
               'catalog_titles': {'relative_root_path'},
               'external_subtitles': {'relative_path'},
               'unresolved_external_subtitles': {'relative_path'}}
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        return {t.name: db.execute('SELECT '+','.join(c.name for c in t.columns
                    if c.name not in allowed.get(t.name, set()))+' FROM '+t.name+
                    ' ORDER BY '+','.join(c.name for c in t.primary_key.columns)).fetchall()
                for t in Base.metadata.sorted_tables}


def patch_scratch(path, projection):
    """Exact future physical-locator patches: no ORM onupdate or scanner lifecycle."""
    with sqlite3.connect(path) as db:
        for locator in projection.locators:
            table = 'catalog_collections' if locator.object_kind == 'collection' else 'catalog_titles'
            db.execute(f'UPDATE {table} SET relative_root_path=? WHERE id=?',
                       (locator.target_relative_path, locator.object_id))
        for row in projection.plan.records:
            if row.object_kind == 'video':
                db.execute('UPDATE videos SET relative_path=?, root_folder=? WHERE id=?',
                    (row.source_relative_path, PurePosixPath(row.source_relative_path).parts[0], row.object_id))
            elif row.object_kind == 'subtitle':
                db.execute('UPDATE external_subtitles SET relative_path=? WHERE id=?',
                           (row.source_relative_path, row.object_id))
            elif row.object_kind == 'confirmed_no_match':
                db.execute('UPDATE unresolved_external_subtitles SET relative_path=? WHERE id=?',
                           (row.source_relative_path, row.object_id))


@pytest.fixture
def scratch_case(session, tmp_path):
    paths=[]
    for name, count in [('Bungo', 13), ('Tenki', 2), ('Uzaki', 13)]:
        root=collection(session, name)
        main=title(session, root, metadata=False)
        attach_metadata(main, str(main.id), name)
        for n in range(1, count+1):
            primary=Video(catalog_title=main, catalog_collection=root, root_folder=name,
                relative_path=f'{name}/Release - {n:02}.mkv', filename=f'Release - {n:02}.mkv',
                size=1, mtime_ns=1, file_type='episode', local_episode_number=n, season_episode_number=n)
            session.add(primary); session.flush()
            secondary=Video(catalog_title=main, catalog_collection=root, root_folder=name,
                relative_path=f'{name}/copy/Release - {n:02}.mkv', filename=primary.filename,
                size=1, mtime_ns=1, file_type='episode', local_episode_number=n, season_episode_number=n,
                duplicate_of_video_id=primary.id)
            session.add(secondary); session.flush()
            paths.extend((primary.relative_path, secondary.relative_path))
        if name != 'Uzaki':
            paths.append(f'{name}/copy/Scan/cover.png')
        else:
            paths.extend(('Uzaki/Uzaki.zip', 'Uzaki/copy/Uzaki.zip'))
    session.add(UnresolvedExternalSubtitle(relative_path='Bungo/missing.ass', filename='Original evidence.ass',
                extension='.ass', status='confirmed_no_match'))
    paths.append('Bungo/missing.ass')
    session.commit()
    source=tmp_path/'source.db'
    scratch=tmp_path/'scratch.db'
    with sqlite3.connect(source) as db:
        session.connection().connection.driver_connection.backup(db)
    with sqlite3.connect(source) as src, sqlite3.connect(scratch) as dst:
        src.backup(dst)
    with readonly_planner_session(source) as ro:
        ctx=load_planner_context(ro)
    snap=FilesystemSnapshot('/library', tuple(FilesystemEntry(p, 'regular', 1, 1,
            content_sha256='uzaki-identical-archive' if p.endswith('.zip') else None) for p in paths), 255, 4096)
    plan=plan_library(ctx, snap)
    from app.target_planner_post_state import simulate_post_state
    projection=simulate_post_state(ctx, snap, plan)
    patch_scratch(scratch, projection)
    # Production inventory deliberately does not rehash the primary ZIP in Subs.
    post_snapshot=replace(projection.snapshot, entries=tuple(
        replace(e, content_sha256=None) if e.relative_path.startswith('Subs/') else e
        for e in projection.snapshot.entries))
    return source, scratch, ctx, plan, projection, post_snapshot


def reload_context(path, monkeypatch):
    import app.scanner.service as scanner
    monkeypatch.setattr(scanner, 'scan_library', lambda *a, **k: pytest.fail('Verifier called scanner'))
    with readonly_planner_session(path) as ro:
        statements=[]
        def select_only(conn, cursor, sql, *args):
            assert sql.lstrip().upper().startswith('SELECT'), sql
            statements.append(sql)
        event.listen(ro.get_bind(), 'before_cursor_execute', select_only)
        ctx=load_planner_context(ro)
        assert len(statements) == 11
        assert not (ro.new or ro.dirty or ro.deleted)
    return ctx


def test_fifteen_incomplete_secondaries_stay_unknown_after_real_reload_without_evidence(scratch_case, monkeypatch):
    source, scratch, ctx, before, projection, snap=scratch_case
    ids={v.id for v in ctx.videos if v.duplicate_primary_id and not v.source.startswith('Uzaki/')}
    assert len(ids) == 15
    for row in before.records:
        if row.object_kind == 'video' and row.object_id in ids:
            assert accounting(row) == 'INCOMPLETE'
    real=plan_library(reload_context(scratch, monkeypatch), snap)
    for row in real.records:
        if row.object_kind == 'video' and row.object_id in ids:
            assert accounting(row) == 'UNKNOWN'
            assert row.duplicate.side_assets_accounted is False
    assert domain_rows(source) == domain_rows(scratch)


def test_real_reload_with_carried_evidence_matches_pure_fixed_point(scratch_case, monkeypatch):
    source, scratch, ctx, before, projection, snap=scratch_case
    import app.target_planner_post_state as api
    assert hasattr(api, 'verify_post_state'), 'Explicit post-state evidence API missing'
    reloaded=reload_context(scratch, monkeypatch)
    # Simulate the serialization boundary without implementing a manifest.
    payload=json.loads(json.dumps([asdict(e) for e in projection.execution_evidence]))
    evidence=tuple(DuplicateExecutionEvidence(**{**e, 'provenance':tuple(e['provenance']),
        'known_side_assets':tuple(SideAssetEvidence(**s) for s in e['known_side_assets'])}) for e in payload)
    assert evidence == projection.execution_evidence
    real=api.verify_post_state(reloaded, snap, execution_evidence=evidence)
    pure=api.verify_post_state(projection.context, snap, execution_evidence=evidence)
    assert real == pure  # records, diagnostics, accounting, provenance, actions, collisions, hashes
    assert project_container_locators(reloaded) == projection.locators
    assert reloaded == replace(projection.context, execution_evidence=())
    duplicates=[r for r in real.records if r.object_kind == 'video' and r.duplicate]
    assert sum(accounting(r) == 'INCOMPLETE' for r in duplicates) == 15
    archive=next(r for r in real.records if r.source_relative_path.endswith('/Uzaki.zip') and r.duplicate)
    assert archive.object_kind == 'duplicate_side_asset' and archive.action == 'KEEP'
    assert archive.authority and archive.duplicate.side_asset_accounting == 'COMPLETE'
    proof=next(s for e in evidence for s in e.known_side_assets if s.source == archive.source_relative_path)
    assert proof.primary_archive_source == 'Subs/Uzaki.zip'
    assert proof.expected_sha256 == proof.primary_archive_sha256 == 'uzaki-identical-archive'
    assert all(r.action == 'KEEP' for r in real.records) and not real.collisions
    assert domain_rows(source) == domain_rows(scratch)
    second=api.simulate_post_state(projection.context, projection.snapshot, projection.plan)
    assert second == projection


def test_uzaki_real_reload_without_provenance_is_review_and_never_purge_safe(scratch_case, monkeypatch):
    source, scratch, ctx, before, projection, snap=scratch_case
    real=plan_library(reload_context(scratch, monkeypatch), snap)
    archive=next(r for r in real.records if r.source_relative_path.startswith('Duplicates/Uzaki/') and r.source_relative_path.endswith('.zip'))
    assert archive.status == 'REVIEW' and archive.target_relative_path is None and archive.duplicate is None
    assert {r.source_relative_path for r in real.records} == {r.target_relative_path for r in before.records}
    for row in real.records:
        if row.duplicate:
            assert accounting(row) == 'UNKNOWN'
            assert row.duplicate.side_assets_accounted is False
            assert row.duplicate.purge_state == 'PRE_EXECUTION_REQUIRED'
    assert domain_rows(source) == domain_rows(scratch)


def test_real_loader_does_not_promote_quarantine_root_to_collection_locator(scratch_case, monkeypatch):
    source, scratch, ctx, before, projection, snap=scratch_case
    real=reload_context(scratch, monkeypatch)
    assert real.collections == projection.context.collections
    assert all('Duplicates' not in c.locators for c in real.collections)
    assert real.titles == projection.context.titles


@pytest.mark.parametrize('change', ['primary_id', 'secondary_id', 'asset_owner', 'empty_provenance'])
def test_carried_execution_evidence_cannot_override_db_relationships(scratch_case, monkeypatch, change):
    source, scratch, ctx, before, projection, snap=scratch_case
    from app.target_planner_post_state import verify_post_state
    evidence=next(e for e in projection.execution_evidence if any(s.object_kind == 'duplicate_side_asset' for s in e.known_side_assets))
    if change == 'primary_id':
        evidence=replace(evidence, primary_video_id=999999)
    elif change == 'secondary_id':
        evidence=replace(evidence, secondary_video_id=999999)
    elif change == 'asset_owner':
        evidence=replace(evidence, known_side_assets=tuple(replace(s, secondary_video_id=999999) for s in evidence.known_side_assets))
    else:
        evidence=replace(evidence, provenance=())
    with pytest.raises(ValueError, match='evidence'):
        verify_post_state(reload_context(scratch, monkeypatch), snap, execution_evidence=(evidence,))


def test_missing_or_changed_carried_archive_is_visible_and_not_complete(scratch_case, monkeypatch):
    source, scratch, ctx, before, projection, snap=scratch_case
    from app.target_planner_post_state import verify_post_state
    proof=next(s for e in projection.execution_evidence for s in e.known_side_assets if s.object_kind == 'duplicate_side_asset')
    reloaded=reload_context(scratch, monkeypatch)
    missing=replace(snap, entries=tuple(e for e in snap.entries if e.relative_path != proof.source))
    changed=replace(snap, entries=tuple(replace(e, content_sha256='changed') if e.relative_path == proof.source else e for e in snap.entries))
    for snapshot in (missing, changed):
        real=verify_post_state(reloaded, snapshot, execution_evidence=projection.execution_evidence)
        archive=next(r for r in real.records if r.source_relative_path == proof.source)
        assert archive.status in {'REVIEW', 'BLOCKED'}
        secondary=next(r for r in real.records if r.object_kind == 'video' and r.object_id == proof.secondary_video_id)
        assert accounting(secondary) == 'INCOMPLETE' and not secondary.duplicate.side_assets_accounted
