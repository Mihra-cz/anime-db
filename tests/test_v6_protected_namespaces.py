"""Root IMPORT stays outside ordinary discovery and V6 execution authority."""
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app.models import ExternalSubtitle, UnresolvedExternalSubtitle, Video
from app.scanner import LibrarySafetyError, scan_library
from app.scanner.service import iter_external_subtitles, iter_videos
from app.target_execution_manifest import manifest_to_json, preflight_execution_manifest
from app.target_planner import project_targets
from app.target_planner_coverage import report_coverage
from app.target_planner_filesystem import inventory_filesystem, plan_library
from app.target_planner_types import FilesystemEntry, FilesystemSnapshot
from test_scanner_reorganization import PROBE
from test_target_planner import context
from test_v6_execution_filesystem import changed_action, seal
from test_v6_execution_manifest import DB, fixture, make
from v6_database_case import database_rows


def write(root, locator, content=b'local fixture'):
    path = root / locator
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize('name', ['IMPORT', 'import', 'iMpOrT'])
def test_scanner_discovery_ignores_only_root_import(tmp_path, name):
    for folder in (name, 'Show/Import', 'IMPORTed', 'Duplicates/Show'):
        write(tmp_path, folder + '/01.mkv')
        write(tmp_path, folder + '/01.srt')
    expected = {'Show/Import/01', 'IMPORTed/01', 'Duplicates/Show/01'}
    assert {p.relative_to(tmp_path).with_suffix('').as_posix() for p in iter_videos(tmp_path)} == expected
    assert {p.relative_to(tmp_path).with_suffix('').as_posix() for p in iter_external_subtitles(tmp_path)} == expected


def test_scan_never_registers_import_assets(tmp_path, monkeypatch):
    root = tmp_path / 'Anime'
    write(root, 'IMPORT/batch/01.mkv')
    write(root, 'IMPORT/batch/01.srt')
    write(root, 'Show/Import/01.mkv')
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE)
    path = tmp_path / 'catalog.sqlite'
    engine = make_engine(f'sqlite:///{path}')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            result = scan_library(session, root)
            assert (result.found, result.created, result.removed) == (1, 1, 0)
            assert list(session.scalars(select(Video.relative_path))) == ['Show/Import/01.mkv']
            assert not list(session.scalars(select(ExternalSubtitle)))
            assert not list(session.scalars(select(UnresolvedExternalSubtitle)))
        assert (root / 'IMPORT/batch/01.mkv').read_bytes() == b'local fixture'
    finally:
        engine.dispose()


@pytest.mark.parametrize('kind', ['video', 'subtitle', 'unresolved_subtitle'])
def test_scan_preserves_existing_import_rows_by_stopping(tmp_path, monkeypatch, kind):
    root = tmp_path / 'Anime'
    write(root, 'Show/01.mkv')
    write(root, 'IMPORT/batch/incoming.mkv')
    monkeypatch.setattr('app.scanner.service.probe_video', lambda *a, **k: PROBE)
    path = tmp_path / 'catalog.sqlite'
    engine = make_engine(f'sqlite:///{path}')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            assert scan_library(session, root).created == 1
            if kind == 'video':
                session.add(Video(relative_path='iMpOrT/batch/incoming.mkv', root_folder='iMpOrT',
                    filename='incoming.mkv', size=13, mtime_ns=1))
            elif kind == 'subtitle':
                session.add(ExternalSubtitle(relative_path='IMPORT/batch/incoming.srt', codec='srt'))
            else:
                session.add(UnresolvedExternalSubtitle(relative_path='IMPORT/batch/incoming.srt',
                    filename='incoming.srt', extension='.srt'))
            session.commit()
        before = database_rows(path)
        with Session(engine) as session:
            with pytest.raises(LibrarySafetyError, match='IMPORT'):
                scan_library(session, root, confirm_deletions=True)
        assert database_rows(path) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize('empty', [True, False])
def test_import_inventory_never_descends_and_coverage_excludes_root(tmp_path, monkeypatch, empty):
    root = tmp_path / 'Anime'
    (root / 'iMpOrT').mkdir(parents=True)
    if not empty:
        write(root, 'iMpOrT/batch/incoming.mkv')
        write(root, 'iMpOrT/batch/archive.zip')
    write(root, 'Show/Import/file.mkv')
    import os
    scandir = os.scandir
    def check(path):
        assert Path(path) != root / 'iMpOrT', 'inventory descended into protected IMPORT'
        return scandir(path)
    monkeypatch.setattr('app.target_planner_filesystem.os.scandir', check)
    snapshot = inventory_filesystem(root)
    entries = {e.relative_path: e for e in snapshot.entries}
    assert entries['iMpOrT'].kind == 'system_excluded'
    assert not any(p.startswith('iMpOrT/') for p in entries)
    assert entries['Show/Import/file.mkv'].kind == 'regular'
    coverage = report_coverage(snapshot, plan_library(context(), snapshot))
    directory = next(d for d in coverage.directories if d.path == 'iMpOrT')
    assert (directory.disposition, directory.contents_inventoried, directory.empty) == ('EXCLUDED', False, None)


def test_planner_rejects_import_as_anime_root():
    ctx = context()
    choices = {('collection', 1): replace(ctx.naming.choices[('collection', 1)], physical_text='iMpOrT')}
    ctx = replace(ctx, naming=replace(ctx.naming, choices=choices))
    row = next(r for r in project_targets(ctx) if r.object_kind == 'video')
    assert row.status == 'REVIEW' and row.target_relative_path is None
    assert 'reserved_library_namespace' in row.blockers


def test_coverage_never_proposes_import_cleanup_from_directory_only_snapshot():
    snapshot = FilesystemSnapshot('/library', (FilesystemEntry('IMPORT', 'directory', mtime_ns=1),), 255, 4096)
    coverage = report_coverage(snapshot, plan_library(context(), snapshot))
    directory = next(d for d in coverage.directories if d.path == 'IMPORT')
    assert directory.disposition == 'EXCLUDED'
    assert directory.contents_inventoried is False and directory.empty is None


def test_planner_refuses_known_import_source_even_with_fabricated_active_snapshot():
    ctx, snapshot = fixture(source='IMPORT/batch/01.mkv')
    plan = plan_library(ctx, snapshot)
    row = next(r for r in plan.records if r.object_kind == 'video')
    assert row.status in {'REVIEW', 'BLOCKED'} and row.target_relative_path is None


@pytest.mark.parametrize('field', ['source', 'target', 'patch_before', 'patch_after'])
def test_resealed_import_manifest_cannot_gain_execution_authority(field):
    ctx, snapshot = fixture()
    original = make(ctx, snapshot)
    if field in {'source', 'target'}:
        action = changed_action(original, **{field: 'iMpOrT/batch/01.mkv'})
        bad = seal(original, (action,))
    else:
        patches = list(original.payload.locator_patches)
        index = next(i for i, p in enumerate(patches) if p.object_kind == 'video' and p.field == 'relative_path')
        patches[index] = replace(patches[index], **{field.removeprefix('patch_'): 'IMPORT/batch/01.mkv'})
        bad = seal(original, locator_patches=tuple(patches))
    with pytest.raises(ValueError, match='IMPORT'):
        manifest_to_json(bad)
    result = preflight_execution_manifest(bad, ctx, snapshot, DB)
    assert result.outcome == 'ERROR'
    assert any('IMPORT' in reason for reason in result.diagnostics)


def test_nested_import_paths_remain_valid_manifest_sources():
    ctx, snapshot = fixture(source='Show/Import/01.mkv')
    manifest = make(ctx, snapshot)
    assert manifest.payload.actions[0].source == 'Show/Import/01.mkv'
    assert manifest_to_json(manifest)
