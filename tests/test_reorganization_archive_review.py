"""Explicit archive decisions share RO preview and manifest planner inputs."""
import json
import re

import pytest

from app.models import Video
from app.target_execution_manifest import build_execution_manifest, manifest_from_json, manifest_to_json
from app.tools.execution_manifest import _read_current
from test_reorganization_web import web, fingerprint
from test_target_planner_service import seed


@pytest.fixture
def archive(web):
    app, client, database, media = web
    with app.state.sessions() as session:
        root, title, videos = seed(session, count=1)
        primary = videos[0]
        secondary = Video(catalog_collection_id=root.id, catalog_title_id=title.id,
            relative_path='Source/copy/video-copy.mkv', root_folder='Source', filename='video-copy.mkv',
            size=1, mtime_ns=1, file_type='episode', season_episode_number=1,
            local_episode_number=1, duplicate_of_video_id=primary.id)
        session.add(secondary)
        session.commit()
        ids = primary.id, secondary.id
    for name in ('Source/video1.mkv', 'Source/video1.ass', 'Source/copy/video-copy.mkv',
                 'Source/copy/Uzaki-chan - všechny titulky.zip'):
        path = media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic')
    return app, client, database, media, ids, name


def preview(response):
    assert response.status_code == 200, response.text
    return json.loads(re.search(r'id="reorganization-data">(.*?)</script>', response.text, re.S).group(1))


def row(data, source):
    return next(r for r in data['plan']['records'] if r['source_relative_path'] == source)


def submit(client, data, source, choice, secondary_id=None):
    review = data['archive_reviews'][source]
    return client.post('/reorganization/archive-review', data={
        'source': source, 'fingerprint': review['fingerprint'], 'choice': choice,
        'secondary_video_id': str(secondary_id or ''),
    })


def test_existing_review_can_be_assigned_changed_and_cancelled_across_workers(archive):
    app, client, database, media, (primary, secondary), source = archive
    before = {p: fingerprint(p) for p in (database, *media.rglob('*')) if p.is_file()}
    initial = preview(client.post('/reorganization/preview'))
    assert row(initial, source)['blockers'] == ['duplicate_side_asset_association_unresolved']
    assert source in initial.get('archive_reviews', {}), 'REVIEW has no human resolution'
    candidates = initial['archive_reviews'][source]['candidates']
    assert [c['id'] for c in candidates] == [secondary]
    assert candidates[0]['source'] == 'Source/copy/video-copy.mkv'
    assert candidates[0]['primary_id'] == primary
    assert initial['archive_reviews'][source]['standalone_allowed']
    assigned = preview(submit(client, initial, source, 'secondary', secondary))
    target = 'Duplicates/Canonical/Season 01/Uzaki-chan - všechny titulky.zip'
    assert row(assigned, source)['status'] == 'READY'
    assert row(assigned, source)['target_relative_path'] == target
    fresh = preview(client.post('/reorganization/preview'))  # New subprocess / persisted reload.
    assert row(fresh, source) == row(assigned, source)
    context, snapshot, plan, db, metrics = _read_current(database, media, None)
    manifest = manifest_from_json(manifest_to_json(build_execution_manifest(context, snapshot, plan, db)))
    action = next(a for a in manifest.payload.actions if a.source == source)
    assert action.target == target and action.action == 'QUARANTINE'
    assert not manifest.payload.generation_diagnostics
    assert manifest.payload.duplicate_evidence[0].known_side_assets[0].provenance == 'explicit_human_secondary_archive'
    assert metrics['sql_select_count'] == 11 and metrics['db_unchanged']
    standalone = preview(submit(client, fresh, source, 'standalone'))
    assert row(standalone, source)['status'] == 'READY'
    assert row(standalone, source)['target_relative_path'] == 'Subs/Uzaki-chan - všechny titulky.zip'
    context, snapshot, plan, db, _ = _read_current(database, media, None)
    manifest = build_execution_manifest(context, snapshot, plan, db)
    assert not manifest.payload.generation_diagnostics
    assert next(a for a in manifest.payload.actions if a.source == source).target == row(standalone, source)['target_relative_path']
    cancelled = preview(submit(client, standalone, source, 'unresolved'))
    assert row(cancelled, source)['status'] == 'REVIEW'
    assert row(preview(client.post('/reorganization/preview')), source)['status'] == 'REVIEW'
    assert {p: fingerprint(p) for p in before} == before


def test_saved_decision_stays_review_when_file_or_db_authority_changes(archive):
    app, client, database, media, (_, secondary), source = archive
    initial = preview(client.post('/reorganization/preview'))
    preview(submit(client, initial, source, 'secondary', secondary))
    path = media / source
    path.write_bytes(b'changed archive')
    changed = preview(client.post('/reorganization/preview'))
    assert row(changed, source)['status'] == 'REVIEW'
    assert 'archive_review_source_changed' in row(changed, source)['blockers']
    preview(submit(client, changed, source, 'secondary', secondary))
    with app.state.sessions() as session:
        session.get(Video, secondary).duplicate_of_video_id = None
        session.commit()
    changed = preview(client.post('/reorganization/preview'))
    assert row(changed, source)['status'] == 'REVIEW'
    assert 'archive_review_duplicate_changed' in row(changed, source)['blockers']
    assert changed['archive_reviews'][source]['decision']['choice'] == 'secondary'
    cancelled = preview(submit(client, changed, source, 'unresolved'))
    assert source not in cancelled['archive_reviews']


def test_invalid_candidate_stale_form_and_collision_do_not_persist(archive):
    app, client, database, media, (primary, secondary), source = archive
    initial = preview(client.post('/reorganization/preview'))
    assert submit(client, initial, source, 'secondary', primary).status_code == 409
    assert row(preview(client.post('/reorganization/preview')), source)['status'] == 'REVIEW'
    (media / source).write_bytes(b'source changed')
    assert submit(client, initial, source, 'secondary', secondary).status_code == 409
    (media / 'Subs').mkdir()
    (media / 'Subs' / source.rsplit('/', 1)[1]).write_bytes(b'occupied')
    current = preview(client.post('/reorganization/preview'))
    assert not current['archive_reviews'][source]['standalone_allowed']
    assert submit(client, current, source, 'standalone').status_code == 409
    assert row(preview(client.post('/reorganization/preview')), source)['status'] == 'REVIEW'
    assert not database.with_name(database.name + '.archive-reviews').exists()


def test_review_post_requires_same_origin_and_cannot_resolve_other_review(archive):
    _, client, _, media, (_, secondary), source = archive
    response = client.post('/reorganization/archive-review', headers={'origin': 'https://elsewhere.invalid'},
        data={'source': source, 'choice': 'secondary', 'secondary_video_id': secondary})
    assert response.status_code == 403
    (media / 'unknown.mkv').write_bytes(b'unknown')
    initial = preview(client.post('/reorganization/preview'))
    assert 'unknown.mkv' not in initial['archive_reviews']
    assert client.post('/reorganization/archive-review', data={
        'source': 'unknown.mkv', 'choice': 'standalone', 'fingerprint': 'invented'}).status_code == 409


def direct_decision(database, media, source, choice, secondary_id=None):
    from app.reorganization_web import build_preview
    data = build_preview(database, media)
    request = dict(source=source, fingerprint=data['archive_reviews'][source]['fingerprint'],
                   choice=choice, secondary_video_id=secondary_id)
    return build_preview(database, media, decision=request)


def test_saved_target_becoming_occupied_stays_review_and_manifest_is_not_actionable(archive):
    from app.reorganization_web import build_preview
    _, _, database, media, _, source = archive
    direct_decision(database, media, source, 'standalone')
    (media / 'Subs').mkdir()
    (media / 'Subs' / source.rsplit('/', 1)[1]).write_bytes(b'collision')
    data = build_preview(database, media)
    archive_row = row(data, source)
    assert archive_row['status'] == 'REVIEW'
    assert archive_row['target_relative_path'] is None
    assert 'archive_review_target_invalid' in archive_row['blockers']
    context, snapshot, plan, db, _ = _read_current(database, media, None)
    manifest = build_execution_manifest(context, snapshot, plan, db)
    assert manifest.payload.generation_diagnostics
    assert all(action.source != source for action in manifest.payload.actions)


def test_removed_source_stays_review_and_decision_can_be_cancelled(archive):
    from app.reorganization_web import build_preview
    _, _, database, media, (_, secondary), source = archive
    direct_decision(database, media, source, 'secondary', secondary)
    (media / source).unlink()
    data = build_preview(database, media)
    archive_row = row(data, source)
    assert archive_row['status'] == 'REVIEW'
    assert 'archive_review_source_changed' in archive_row['blockers']
    result = direct_decision(database, media, source, 'unresolved')
    assert source not in result['archive_reviews']


def test_mtime_change_is_not_silently_accepted_and_get_preserves_saved_sidecar(archive):
    import os
    from app.reorganization_web import build_preview
    _, client, database, media, (_, secondary), source = archive
    direct_decision(database, media, source, 'secondary', secondary)
    sidecar = next(database.with_name(database.name + '.archive-reviews').glob('*.json'))
    before = fingerprint(sidecar), fingerprint(database)
    assert client.get('/reorganization').status_code == 200
    assert (fingerprint(sidecar), fingerprint(database)) == before
    path = media / source
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
    assert 'archive_review_source_changed' in row(build_preview(database, media), source)['blockers']


def test_switching_between_explicit_secondaries_keeps_exact_selected_video(archive):
    from app.reorganization_web import build_preview
    app, _, database, media, (primary, secondary), source = archive
    direct_decision(database, media, source, 'secondary', secondary)
    with app.state.sessions() as session:
        old = session.get(Video, secondary)
        other = Video(catalog_collection_id=old.catalog_collection_id, catalog_title_id=old.catalog_title_id,
            relative_path='Source/copy/other-copy.mkv', root_folder='Source', filename='other-copy.mkv',
            size=1, mtime_ns=1, file_type='episode', season_episode_number=1,
            local_episode_number=1, duplicate_of_video_id=primary)
        session.add(other)
        session.commit()
        other_id = other.id
    (media / 'Source/copy/other-copy.mkv').write_bytes(b'synthetic')
    changed = direct_decision(database, media, source, 'secondary', other_id)
    assert row(changed, source)['status'] == 'READY'
    assert row(changed, source)['duplicate']['secondary_video_id'] == other_id
    assert row(build_preview(database, media), source)['duplicate']['secondary_video_id'] == other_id


@pytest.mark.parametrize('choice', ['secondary', 'standalone'])
def test_real_manifest_cli_uses_persisted_web_decision_and_preflight_detects_cancel(archive, choice):
    from app.tools.execution_manifest import main
    from app.target_execution_manifest import preflight_execution_manifest
    _, _, database, media, (_, secondary), source = archive
    decided = direct_decision(database, media, source, choice, secondary)
    output = database.parent / 'manifest.json'
    assert main(['generate-manifest', '--db', str(database), '--library-root', str(media), '--output', str(output)]) == 0
    manifest = manifest_from_json(output.read_text())
    assert next(a for a in manifest.payload.actions if a.source == source).target == row(decided, source)['target_relative_path']
    assert not manifest.payload.generation_diagnostics
    context, snapshot, _, db, _ = _read_current(database, media, None)
    assert preflight_execution_manifest(manifest, context, snapshot, db).outcome != 'STALE'
    direct_decision(database, media, source, 'unresolved')
    context, snapshot, _, db, _ = _read_current(database, media, None)
    assert preflight_execution_manifest(manifest, context, snapshot, db).outcome == 'STALE'


@pytest.fixture
def uzaki(web):
    """Thirteen confirmed secondary episodes and two small identical ZIPs."""
    import io
    from zipfile import ZipFile
    app, client, database, media = web
    with app.state.sessions() as session:
        root, title, primaries = seed(session, count=13)
        secondary_ids = []
        for number, primary in enumerate(primaries, 1):
            video = Video(catalog_collection_id=root.id, catalog_title_id=title.id,
                relative_path=f'Source/copy/video{number}.mkv', root_folder='Source', filename=f'video{number}.mkv',
                size=1, mtime_ns=1, file_type='episode', season_episode_number=number,
                local_episode_number=number, duplicate_of_video_id=primary.id)
            session.add(video)
            session.flush()
            secondary_ids.append(video.id)
        session.commit()
    for number in range(1, 14):
        for name in (f'Source/video{number}.mkv', f'Source/video{number}.ass', f'Source/copy/video{number}.mkv'):
            path = media / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic')
    source = 'Source/copy/Uzaki-chan wa Asobitai! Double - všechny titulky.zip'
    primary_archive = 'Source/Uzaki-chan wa Asobitai! Double - všechny titulky.zip'
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('episode01.srt', 'Synthetic subtitles')
    for name in (source, primary_archive):
        (media / name).write_bytes(stream.getvalue())
    return app, client, database, media, secondary_ids, source, primary_archive


def test_uzaki_preview_matches_full_planner_without_human_decision(uzaki):
    from app.reorganization_web import build_preview
    _, client, database, media, _, source, _ = uzaki
    before = {p: fingerprint(p) for p in (database, *media.rglob('*')) if p.is_file()}
    web = build_preview(database, media)
    archive_row = row(web, source)
    assert archive_row['status'] == 'READY'
    assert archive_row['action'] == 'QUARANTINE'
    assert archive_row['target_relative_path'] == 'Duplicates/Canonical/Season 01/Uzaki-chan wa Asobitai! Double - všechny titulky.zip'
    assert source not in web['archive_reviews']
    assert row(preview(client.post('/reorganization/preview')), source) == json.loads(json.dumps(archive_row))
    context, snapshot, plan, db, _ = _read_current(database, media, None)
    from dataclasses import asdict
    assert asdict(next(r for r in plan.records if r.source_relative_path == source)) == archive_row
    assert dict(plan.status_counts)['REVIEW'] == 0
    manifest = build_execution_manifest(context, snapshot, plan, db)
    assert not manifest.payload.generation_diagnostics
    assert next(a for a in manifest.payload.actions if a.source == source).target == archive_row['target_relative_path']
    assert {p: fingerprint(p) for p in before} == before


def test_selective_preview_hashes_only_matching_archive_pairs_in_one_inventory(uzaki, monkeypatch):
    from app.reorganization_web import build_preview
    from app import target_planner_filesystem as filesystem
    _, _, database, media, _, source, primary_archive = uzaki
    for name in ('Source/unrelated.zip', 'Source/copy/secondary-only.zip', 'Other/unrelated.zip',
                 'Other/huge.mp4', 'Source/copy/unrelated.mkv'):
        path = media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'unrelated')
    opened, visited = [], []
    real_open, real_scandir = filesystem.io.open, filesystem.os.scandir
    def observe_open(path, *args, **kwargs):
        from pathlib import Path
        candidate = Path(path)
        if media in candidate.parents:
            opened.append(candidate.relative_to(media).as_posix())
            assert candidate.suffix.lower() in {'.zip', '.rar', '.7z'}, 'Read non-archive library content'
        return real_open(path, *args, **kwargs)
    def observe_scandir(path):
        from pathlib import Path
        candidate = Path(path)
        if candidate == media or media in candidate.parents:
            visited.append(candidate)
        return real_scandir(path)
    monkeypatch.setattr(filesystem.io, 'open', observe_open)
    monkeypatch.setattr(filesystem.os, 'scandir', observe_scandir)
    assert row(build_preview(database, media), source)['status'] == 'READY'
    assert sorted(opened) == sorted([source, primary_archive])
    assert sorted(visited) == sorted([media, media / 'Source', media / 'Source/copy', media / 'Other'])


@pytest.mark.parametrize('failure', ['different_content', 'different_size', 'invalid_duplicate', 'missing_primary_archive'])
def test_unproved_archive_copy_stays_in_existing_review(uzaki, failure, monkeypatch):
    from app.reorganization_web import build_preview
    from app import target_planner_filesystem as filesystem
    app, _, database, media, secondary_ids, source, primary_archive = uzaki
    if failure == 'different_content':
        content = (media / source).read_bytes()
        (media / source).write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    elif failure == 'different_size':
        (media / source).write_bytes(b'different size')
    elif failure == 'invalid_duplicate':
        with app.state.sessions() as session:
            video = session.get(Video, secondary_ids[0])
            video.season_episode_number = 999
            video.local_episode_number = 999
            session.commit()
    else:
        (media / primary_archive).unlink()
    opened = []
    original_open = filesystem.io.open
    def observe_open(path, *args, **kwargs):
        from pathlib import Path
        if media in Path(path).parents:
            opened.append(Path(path).relative_to(media).as_posix())
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(filesystem.io, 'open', observe_open)
    data = build_preview(database, media)
    assert row(data, source)['status'] == 'REVIEW'
    assert source in data['archive_reviews']
    assert sorted(opened) == (sorted([source, primary_archive]) if failure == 'different_content' else [])
    _, _, plan, _, _ = _read_current(database, media, None)
    assert next(r for r in plan.records if r.source_relative_path == source).status == 'REVIEW'


def test_archive_review_get_redirects_after_save_and_reset(archive):
    app, client, database, media, (_, secondary), source = archive
    initial = preview(client.post('/reorganization/preview'))
    assigned = preview(submit(client, initial, source, 'secondary', secondary))
    response = client.get('/reorganization/archive-review')
    assert response.status_code == 303
    assert response.headers['location'] == '/reorganization'
    assert client.get(response.headers['location']).status_code == 200
    preview(submit(client, assigned, source, 'unresolved'))
    assert client.get('/reorganization/archive-review').status_code == 303


def test_sidecar_directory_listing_error_is_not_an_empty_decision_set(archive, monkeypatch):
    from pathlib import Path
    from app.reorganization_archive_review import decision_directory, load_archive_decisions
    _, _, database, media, (_, secondary), source = archive
    direct_decision(database, media, source, 'secondary', secondary)
    directory = decision_directory(database)
    (directory / 'ignored.tmp').write_text('not an archive decision')
    assert len(load_archive_decisions(database)) == 1
    original = Path.iterdir
    def denied(path):
        if path == directory:
            raise PermissionError(13, 'Permission denied', str(path))
        return original(path)
    monkeypatch.setattr(Path, 'iterdir', denied)
    with pytest.raises(OSError, match='Nelze načíst.*rozhodnutí'):
        load_archive_decisions(database)


def test_unreadable_sidecar_directory_has_clear_http_error_and_preserves_decision(archive):
    import os
    from app.reorganization_archive_review import decision_directory
    if os.geteuid() == 0:
        pytest.skip('EACCES requires an unprivileged process')
    _, client, database, media, (_, secondary), source = archive
    direct_decision(database, media, source, 'secondary', secondary)
    directory = decision_directory(database)
    sidecar = next(directory.iterdir())
    before = fingerprint(sidecar), fingerprint(database)
    directory.chmod(0)
    try:
        response = client.post('/reorganization/preview')
        assert response.status_code == 503
        assert 'Nelze načíst' in response.text and 'rozhodnutí' in response.text
        assert 'REVIEW' in response.text
        assert 'reorganization-data' not in response.text
    finally:
        directory.chmod(0o700)
    assert (fingerprint(sidecar), fingerprint(database)) == before
    assert row(preview(client.post('/reorganization/preview')), source)['status'] == 'READY'
