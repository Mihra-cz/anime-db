"""Local-only web previews use real planner evidence and never mutate sources."""
import hashlib
import subprocess

import pytest

from app.config import Settings
from app.database import Base
from app.main import create_app
from test_naming_review_web import EndpointClient
from test_target_planner_service import seed


def fingerprint(path):
    info = path.stat()
    return info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def web(tmp_path):
    database = tmp_path / 'web.db'
    media = tmp_path / 'media'
    media.mkdir()
    app = create_app(Settings(anime_path=media, database_url=f'sqlite:///{database}',
        metadata_artwork_directory=tmp_path / 'artwork', metadata_download_artwork=False))
    Base.metadata.create_all(app.state.sessions.kw['bind'])
    return app, EndpointClient(app), database, media


def populate(app, media):
    with app.state.sessions() as session:
        _, _, videos = seed(session, count=2)
        for video in videos:
            path = media / video.relative_path
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'video')
            path.with_suffix('.ass').write_text('subtitle')


def test_get_is_database_only_and_links_existing_reviews(web, monkeypatch):
    app, client, database, media = web
    populate(app, media)
    import app.reorganization_web as module
    monkeypatch.setattr(module, 'run_preview', lambda *a, **k: pytest.fail('GET started inventory'))
    before = fingerprint(database)
    response = client.get('/reorganization')
    assert response.status_code == 200
    assert 'Náhled dosud nebyl vytvořen' in response.text
    for url in ('/naming-review', '/naming-review/layout', '/hierarchy-review'):
        assert f'href="{url}"' in response.text
    assert 'Videa: 2' in response.text
    assert fingerprint(database) == before


def test_real_preview_and_sources_unchanged(web):
    app, client, database, media = web
    populate(app, media)
    before = {p: fingerprint(p) for p in (database, *media.rglob('*')) if p.is_file()}
    directories = {p: p.stat().st_mtime_ns for p in media.rglob('*') if p.is_dir()}
    response = client.post('/reorganization/preview')
    assert response.status_code == 200
    assert 'Source/video1.mkv' in response.text
    assert 'Canonical/Season 01/Canonical - S01E01.mkv' in response.text
    assert 'MOVE' in response.text and 'READY' in response.text
    assert 'Vyžaduje rozhodnutí: cleanup nebo warning' in response.text
    assert 'Fyzické spuštění provádí správce na hostiteli' in response.text
    assert {p: fingerprint(p) for p in before} == before
    assert {p: p.stat().st_mtime_ns for p in directories} == directories
    assert set(p for p in media.rglob('*') if p.is_file()) == set(before) - {database}


def test_empty_preview(web):
    _, client, _, _ = web
    response = client.post('/reorganization/preview')
    assert response.status_code == 200
    assert 'Prázdný plán' in response.text
    assert 'Připraveno ke spuštění' not in response.text


def test_missing_library(web):
    _, client, _, media = web
    media.rmdir()
    response = client.post('/reorganization/preview')
    assert response.status_code == 503
    assert 'žádný platný plán' in response.text
    assert 'reorganization-data' not in response.text


def test_conflict_and_unknown_asset(web):
    app, client, _, media = web
    populate(app, media)
    (media / 'Canonical').write_bytes(b'occupied target parent')
    (media / 'unknown.mkv').write_bytes(b'unknown')
    response = client.post('/reorganization/preview')
    assert response.status_code == 200
    assert 'BLOCKED' in response.text and 'REVIEW' in response.text
    assert 'target_collision:file_directory' in response.text
    assert 'unknown.mkv' in response.text


def test_timeout_has_no_partial_plan(web, monkeypatch):
    _, client, _, _ = web
    import app.reorganization_web as module
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired('preview', 1)
    monkeypatch.setattr(module, 'run_preview', timeout)
    response = client.post('/reorganization/preview')
    assert response.status_code == 504
    assert 'časový limit' in response.text
    assert 'Soubory · SOURCE → TARGET' not in response.text


def test_unavailable_db_does_not_create_it(web):
    _, client, database, _ = web
    database.unlink()
    assert client.get('/reorganization').status_code == 503
    assert not database.exists()


def test_preview_admission_is_bounded(web):
    app, client, _, _ = web
    with app.state.reorganization_preview_lock:
        response = client.post('/reorganization/preview')
    assert response.status_code == 409


def test_warning_and_protected_scopes(web):
    from app.models import UnresolvedExternalSubtitle
    app, client, _, media = web
    populate(app, media)
    with app.state.sessions() as session:
        session.add(UnresolvedExternalSubtitle(relative_path='Source/lost03.ass',
            filename='lost03.ass', extension='.ass', status='confirmed_no_match'))
        session.commit()
    (media / 'Source/lost03.ass').write_text('subtitle without video')
    for name in ('IMPORT', '#recycle'):
        directory = media / name
        directory.mkdir()
        (directory / 'do-not-inventory.mkv').write_bytes(b'protected')
    (media / 'Duplicates').mkdir()
    (media / 'Duplicates/unknown.mkv').write_bytes(b'unprovenanced')
    response = client.post('/reorganization/preview')
    assert response.status_code == 200
    assert 'subtitle_present_video_missing' in response.text
    assert 'EXCLUDED' in response.text
    assert 'do-not-inventory.mkv' not in response.text
    assert 'unprovenanced_or_unexpected_technical_lane_asset' in response.text


def test_inaccessible_library_is_incomplete(web, monkeypatch):
    from app import reorganization_web as module
    from app import target_planner_filesystem as filesystem
    app, client, database, media = web
    original = filesystem.os.scandir
    def denied(path):
        if str(path) == str(media):
            raise PermissionError(13, 'denied')
        return original(path)
    monkeypatch.setattr(filesystem.os, 'scandir', denied)
    from app.scanner import LibraryUnavailableError
    with pytest.raises(LibraryUnavailableError):
        module.build_preview(database, media)
    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, 'preview')
    monkeypatch.setattr(module, 'run_preview', unavailable)
    before = fingerprint(database)
    response = client.post('/reorganization/preview')
    assert response.status_code == 503
    assert 'reorganization-data' not in response.text
    assert fingerprint(database) == before


def test_preview_reads_no_file_contents(web, monkeypatch):
    from app import reorganization_web as module
    from app import target_planner_filesystem as filesystem
    app, _, database, media = web
    populate(app, media)
    (media / 'Source/subs.zip').write_bytes(b'archive')
    monkeypatch.setattr(filesystem.io, 'open', lambda *a, **kw: pytest.fail('file content opened'))
    result = module.build_preview(database, media)
    assert result['plan']['records']


def test_preview_is_fresh_and_worker_errors_hide_partial_results(web, monkeypatch):
    app, client, _, media = web
    populate(app, media)
    assert 'source_missing_or_not_regular' not in client.post('/reorganization/preview').text
    (media / 'Source/video1.mkv').unlink()
    assert 'source_missing_or_not_regular' in client.post('/reorganization/preview').text
    from app import reorganization_web as module
    def failed(*a, **kw):
        raise subprocess.CalledProcessError(1, 'preview', output='partial', stderr='private detail')
    monkeypatch.setattr(module, 'run_preview', failed)
    response = client.post('/reorganization/preview')
    assert response.status_code == 503
    assert 'Soubory · SOURCE → TARGET' not in response.text
    assert 'private detail' not in response.text
    # Admission is released on every failure.
    assert not app.state.reorganization_preview_lock.locked()


def test_cross_origin_cannot_trigger_inventory(web, monkeypatch):
    from app import reorganization_web as module
    _, client, _, _ = web
    monkeypatch.setattr(module, 'run_preview', lambda *a, **kw: pytest.fail('cross-origin inventory'))
    response = client.post('/reorganization/preview', headers={'origin': 'https://elsewhere.invalid'})
    assert response.status_code == 403


def test_real_worker_timeout_is_reaped(web, monkeypatch):
    import sys
    from app import reorganization_web as module
    _, _, database, media = web
    processes = []
    real_popen = subprocess.Popen
    def popen(args, **kw):
        child = real_popen([sys.executable, '-c', 'import time; time.sleep(10)'], **kw)
        processes.append(child)
        return child
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    with pytest.raises(subprocess.TimeoutExpired):
        module.run_preview(database, media, timeout=0.1)
    assert len(processes) == 1 and processes[0].poll() is not None



@pytest.mark.parametrize('count', [2, 80])
def test_get_query_count_is_bounded_and_select_only(web, monkeypatch, count):
    from contextlib import contextmanager
    from sqlalchemy import event
    from app import reorganization_web as module
    app, client, _, _ = web
    with app.state.sessions() as session:
        seed(session, count=count)
    original = module.readonly_planner_session
    statements = []
    @contextmanager
    def monitored(path):
        with original(path) as session:
            def record(conn, cursor, statement, *args):
                statements.append(statement)
                assert statement.lstrip().upper().startswith('SELECT')
            event.listen(session.get_bind(), 'before_cursor_execute', record)
            try:
                yield session
            finally:
                event.remove(session.get_bind(), 'before_cursor_execute', record)
    monkeypatch.setattr(module, 'readonly_planner_session', monitored)
    response = client.get('/reorganization')
    assert response.status_code == 200
    assert f'Videa: {count}' in response.text
    assert len(statements) == 4


def test_routes_through_asgi_without_startup_writes(web):
    import asyncio
    import httpx
    app, _, database, media = web
    populate(app, media)
    before = fingerprint(database)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            assert (await client.get('/reorganization')).status_code == 200
            result = await client.post('/reorganization/preview')
            assert result.status_code == 200
            assert 'Canonical - S01E01.mkv' in result.text
            assert result.headers['cache-control'] == 'no-store'
    asyncio.run(exercise())
    assert fingerprint(database) == before


@pytest.mark.parametrize('url, expected', [
    ('sqlite:///file:///data/anime.db?mode=rw&uri=true', '/data/anime.db'),
    ('sqlite:///./data/anime.db', 'data/anime.db'),
])
def test_database_url_matches_execution_guard(url, expected):
    from pathlib import Path
    from app.reorganization_web import database_path
    assert database_path(url) == Path(expected).resolve()


@pytest.mark.parametrize('url', [
    'sqlite://', 'sqlite:///:memory:', 'postgresql:///db',
    'sqlite:///file::memory:?mode=memory&uri=true',
    'sqlite:///file:///tmp/no.db?mode=rwc&uri=true',
    'sqlite:///file://remote/tmp/db?uri=true&mode=rw',
    'sqlite:///file:///tmp/db?vfs=unsafe&uri=true',
    'sqlite:///file:///tmp/db?uri=false',
    'sqlite:///file:///tmp/x%00.db?uri=true',
])
def test_unsafe_database_urls_are_rejected(url):
    from app.reorganization_web import database_path
    with pytest.raises(ValueError):
        database_path(url)


def test_preview_timeout_is_independent(monkeypatch, tmp_path):
    from app.config import get_settings
    monkeypatch.setenv('REORGANIZATION_PREVIEW_TIMEOUT_SECONDS', '240')
    settings = get_settings(tmp_path/'absent.env')
    assert settings.reorganization_preview_timeout_seconds == 240
    assert settings.library_access_timeout_seconds == 10
    for value in ('0', '-1', 'nan', 'inf', '3601', 'bad'):
        monkeypatch.setenv('REORGANIZATION_PREVIEW_TIMEOUT_SECONDS', value)
        with pytest.raises(ValueError):
            get_settings(tmp_path/'absent.env')


def test_coverage_operations_do_not_count_blocked_moves(web):
    from app.reorganization_web import preview_presentation, build_preview
    app, _, database, media = web
    populate(app, media)
    (media/'Canonical').write_bytes(b'collision')
    result = build_preview(database, media)
    view = preview_presentation(result)
    assert view['operations']['MOVE'] == 0
    assert view['operations']['REVIEW'] >= 4
    assert view['state'] == 'FILES_UNRESOLVED'


def test_cleanup_is_not_incomplete_inventory(web):
    from app.reorganization_web import preview_presentation, build_preview
    app, _, database, media = web
    populate(app, media)
    assert preview_presentation(build_preview(database, media))['state'] == 'DECISIONS_REQUIRED'


def test_uri_database_readonly_no_creation(web):
    from app.reorganization_web import database_path, library_counts
    app, _, database, media = web
    populate(app, media)
    before = fingerprint(database)
    uri = f'sqlite:///file://{database}?mode=rw&uri=true'
    assert library_counts(database_path(uri))['Videa'] == 2
    assert fingerprint(database) == before
    missing = database.with_name('missing.db')
    from sqlalchemy.exc import OperationalError
    with pytest.raises(OperationalError):
        library_counts(database_path(f'sqlite:///file://{missing}?mode=rw&uri=true'))
    assert not missing.exists()


def test_require_mount_rejects_unmounted_empty_directory(web):
    from app.reorganization_web import build_preview
    from app.scanner import LibraryUnavailableError
    _, _, database, media = web
    with pytest.raises(LibraryUnavailableError):
        build_preview(database, media, require_mount=True, access_timeout=1)


def test_empty_library_against_nonempty_db_fails_closed(web):
    from app.reorganization_web import build_preview
    from app.scanner import LibraryUnavailableError
    app, _, database, media = web
    with app.state.sessions() as session:
        seed(session, count=2)
    with pytest.raises(LibraryUnavailableError, match='prázdná'):
        build_preview(database, media)


def test_get_preview_redirects_without_planning(web, monkeypatch):
    import app.reorganization_web as module
    _, client, _, _ = web
    monkeypatch.setattr(module, 'run_preview', lambda *a, **kw: pytest.fail('GET inventory'))
    response = client.get('/reorganization/preview')
    assert response.status_code == 303
    assert response.headers['location'] == '/reorganization'


def test_non_utf8_and_html_names_are_safe(web):
    import os
    app, client, _, media = web
    populate(app, media)
    raw = os.fsencode(media) + b'/bad-\xff-<script>alert(1)</script>.mkv'
    # Keep the path one component (slash is not a filename character).
    raw = raw.replace(b'</script>', b'<script>')
    with open(raw, 'wb') as stream:
        stream.write(b'unknown')
    response = client.post('/reorganization/preview')
    assert response.status_code == 200
    assert '\\udcff' in response.text
    assert '<script>alert(1)' not in response.text
    assert '\\u003cscript\\u003e' in response.text
    assert b'\xff' in os.fsencode(os.listdir(media)[-1]) or any(b'\xff' in os.fsencode(n) for n in os.listdir(media))


def test_unreaped_worker_does_not_block_or_admit_another(web, monkeypatch):
    import app.reorganization_web as module
    app, client, _, _ = web
    class Stuck:
        pid = 123456789
        stdout = stderr = None
        returncode = None
        calls = 0
        def communicate(self, *, timeout):
            self.calls += 1
            if self.calls > 1:
                assert timeout <= 1
            raise subprocess.TimeoutExpired('stuck', timeout)
        def wait(self, *, timeout):
            assert timeout <= .1
            raise subprocess.TimeoutExpired('stuck', timeout)
        def poll(self):
            return self.returncode
    process = Stuck()
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: process)
    monkeypatch.setattr(module.os, 'killpg', lambda *a: None)
    response = client.post('/reorganization/preview')
    assert response.status_code == 504 and not app.state.reorganization_preview_lock.locked()
    assert client.post('/reorganization/preview').status_code == 409
    process.returncode = -9
    monkeypatch.setattr(module, 'run_preview', lambda *a, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, 'failed')))
    assert client.post('/reorganization/preview').status_code == 503
    assert app.state.reorganization_stopping_worker is None


def test_worker_diagnostic_is_bounded_and_escaped(web, monkeypatch, caplog):
    from app import reorganization_web as module
    _, _, database, media = web
    class Failed:
        returncode = 1
        def communicate(self, *, timeout):
            return b'partial', b'x'*10000 + b'\n\x1b[31m\xff'
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: Failed())
    with pytest.raises(subprocess.CalledProcessError):
        module.run_preview(database, media, timeout=2)
    assert len(caplog.text) < 9000
    assert '\x1b' not in caplog.text and '\\x1b' in caplog.text


def test_partial_inventory_has_distinct_state(web, monkeypatch):
    from app import reorganization_web as module
    from app.target_planner_types import FilesystemEntry, FilesystemSnapshot
    app, client, database, media = web
    populate(app, media)
    monkeypatch.setattr(module, 'inventory_filesystem', lambda *a, **k: FilesystemSnapshot(str(media),
        (FilesystemEntry('unreadable', 'inaccessible', diagnostic='listing_error:13'),), 255, 4096))
    # This partial snapshot has no regular file, so do not treat it as a valid relocation plan.
    from app.scanner import LibraryUnavailableError
    with pytest.raises(LibraryUnavailableError):
        module.build_preview(database, media)


def test_preview_passes_separate_timeout_and_mount_requirements(web, monkeypatch):
    from app import reorganization_web as module
    _, client, _, _ = web
    received = {}
    def capture(db, root, **kwargs):
        received.update(kwargs)
        raise subprocess.CalledProcessError(1, 'unavailable')
    monkeypatch.setattr(module, 'run_preview', capture)
    assert client.post('/reorganization/preview').status_code == 503
    assert received == dict(timeout=120, require_mount=False, access_timeout=10)


def test_partial_inventory_state_and_coverage_detail_agree(web, monkeypatch):
    from app import reorganization_web as module
    from app.target_planner_types import FilesystemEntry, FilesystemSnapshot
    app, _, database, media = web
    populate(app, media)
    from app.target_planner_filesystem import inventory_filesystem
    snapshot = inventory_filesystem(media, hash_archives=False)
    from dataclasses import replace
    broken = replace(snapshot, entries=snapshot.entries + (FilesystemEntry('partial', 'inaccessible', diagnostic='listing_error:13'),))
    monkeypatch.setattr(module, 'inventory_filesystem', lambda *a, **k: broken)
    result = module.build_preview(database, media)
    view = module.preview_presentation(result)
    assert view['state'] == 'INVENTORY_INCOMPLETE'
    for operation, count in view['operations'].items():
        assert count == sum(row['disposition'] == operation for row in result['coverage']['files'])


def test_default_timeout_and_direct_settings_validation():
    from app.config import Settings
    from pathlib import Path
    assert Settings(Path('/tmp'), 'sqlite://').reorganization_preview_timeout_seconds == 120
    with pytest.raises(ValueError):
        Settings(Path('/tmp'), 'sqlite://', reorganization_preview_timeout_seconds=float('nan'))


def test_full_routes_with_nexus_uri_shape(web):
    from app.config import Settings
    app, _, database, media = web
    populate(app, media)
    before = fingerprint(database)
    uri_app = create_app(Settings(anime_path=media,
        database_url=f'sqlite:///file://{database}?mode=rw&uri=true',
        metadata_artwork_directory=media.parent/'uri-artwork', metadata_download_artwork=False))
    client = EndpointClient(uri_app)
    assert client.get('/reorganization').status_code == 200
    assert client.post('/reorganization/preview').status_code == 200
    assert fingerprint(database) == before
