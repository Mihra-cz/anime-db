"""Real application entry points, exclusively on E6B's explicit scratch binding."""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text

from app.config import Settings
from app.database import Base, make_engine, make_session_factory
from app.main import create_app
from app.models import Video
from app.migrations import migrate_schema, migrate_schema_at_startup
from app.title_locator_migration import migrate_title_locator_persistence
from app.scanner import scan_library
from test_v6_maintenance import api, binding


def settings(gate):
    return Settings(anime_path=gate.library_root,
        database_url=f'sqlite:///{gate.database_path}', metadata_enabled=False,
        metadata_download_artwork=False,
        metadata_artwork_directory=gate.database_path.parent / 'artwork')


@pytest.mark.parametrize('finish', ['commit', 'rollback'])
def test_lease_is_held_inside_and_after_actual_dbapi_completion(binding, finish):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    calls, armed = [], []
    class CheckedConnection(sqlite3.Connection):
        def complete(self, method):
            if armed and method == finish:
                with pytest.raises(api().MaintenanceBusy):
                    with gate.maintenance():
                        pytest.fail('admission released before DBAPI completion')
                calls.append('before')
            result = getattr(super(), method)()
            if armed and method == finish:
                with pytest.raises(api().MaintenanceBusy):
                    with gate.maintenance():
                        pytest.fail('admission released inside DBAPI completion')
                calls.append('after')
            return result
        def commit(self):
            return self.complete('commit')
        def rollback(self):
            return self.complete('rollback')
    @event.listens_for(engine, 'do_connect')
    def connect(dialect, record, args, kwargs):
        kwargs['factory'] = CheckedConnection
    try:
        with make_session_factory(engine)() as session:
            session.execute(text("UPDATE sample SET value='changed'"))
            armed.append(True)
            getattr(session, finish)()
            armed.clear()
        assert calls and calls[:2] == ['before', 'after']
        with gate.maintenance():
            pass
    finally:
        engine.dispose()


def test_web_edits_are_refused_reads_work_and_operation_resumes(binding):
    gate = binding
    app = create_app(settings(gate), maintenance=gate)
    with TestClient(app) as client:
        with app.state.sessions() as session:
            video = Video(relative_path='old.mkv', root_folder='.', filename='old.mkv', size=3, mtime_ns=1)
            session.add(video)
            session.commit()
            video_id = video.id
        before = gate.database_path.read_bytes()
        with gate.maintenance():
            response = client.post(f'/videos/{video_id}/hardsub',
                data={'mode': 'cs', 'filter_name': 'all'}, follow_redirects=False)
            assert response.status_code == 503
            assert client.post('/scan', follow_redirects=False).status_code == 503
            for route in ('/health', '/', '/hierarchy-review', '/metadata-review'):
                assert client.get(route).status_code == 200
            assert gate.database_path.read_bytes() == before
        response = client.post(f'/videos/{video_id}/hardsub',
            data={'mode': 'cs', 'filter_name': 'all'}, follow_redirects=False)
        assert response.status_code == 303
        with app.state.sessions() as session:
            assert session.get(Video, video_id).manual_hardsub_cs is True


@pytest.mark.parametrize('migration', [migrate_schema, migrate_schema_at_startup, migrate_title_locator_persistence])
def test_public_migration_entry_points_do_not_touch_database_during_maintenance(binding, migration):
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    Base.metadata.create_all(engine)
    before = gate.database_path.read_bytes()
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda c, cur, sql, p, ctx, many: statements.append(sql))
    try:
        with gate.maintenance(), pytest.raises(api().MaintenanceBusy):
            migration(engine)
        with gate.maintenance(), pytest.raises(api().MaintenanceBusy):
            migration(engine=engine)
        assert not statements and gate.database_path.read_bytes() == before
    finally:
        engine.dispose()


def test_lifespan_refuses_even_fresh_startup_until_exclusion_ends(binding):
    gate = binding
    app = create_app(settings(gate), maintenance=gate)
    before = gate.database_path.read_bytes()
    with gate.maintenance(), pytest.raises(api().MaintenanceBusy):
        with TestClient(app):
            pytest.fail('startup entered during maintenance')
    assert gate.database_path.read_bytes() == before
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200


def test_scanner_is_refused_before_inventory_and_then_holds_whole_workflow(binding, monkeypatch):
    from app.scanner import service
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    before = gate.database_path.read_bytes()
    scan = service._scan_library
    def forbidden(*args, **kwargs):
        pytest.fail('scanner inventory started during maintenance')
    try:
        with monkeypatch.context() as blocked:
            blocked.setattr(service, '_scan_library', forbidden)
            with gate.maintenance(), sessions() as session, pytest.raises(api().MaintenanceBusy):
                scan_library(session, gate.library_root)
        assert gate.database_path.read_bytes() == before
        source = gate.library_root / 'Show' / 'Show - 01.mkv'
        source.parent.mkdir(mode=0o700)
        source.write_bytes(b'video')
        monkeypatch.setattr(service, 'probe_video', lambda *a, **k: {
            'duration': 60., 'video_codec': 'h264', 'width': 1920, 'height': 1080,
            'audio': [], 'subtitles': []})
        def whole_workflow(*args, **kwargs):
            with pytest.raises(api().MaintenanceBusy):
                with gate.maintenance():
                    pytest.fail('scanner has no admission before its first transaction')
            result = scan(*args, **kwargs)
            with pytest.raises(api().MaintenanceBusy):
                with gate.maintenance():
                    pytest.fail('scanner lease ended before workflow completed')
            return result
        monkeypatch.setattr(service, '_scan_library', whole_workflow)
        with sessions() as session:
            assert scan_library(session, gate.library_root).created == 1
        with gate.maintenance():
            pass
        with sessions() as session:
            assert session.scalar(select(Video)).relative_path == 'Show/Show - 01.mkv'
    finally:
        engine.dispose()


def test_default_configuration_does_not_enable_maintenance(binding):
    engine = make_engine(f'sqlite:///{binding.database_path}')
    try:
        assert 'scratch_maintenance' not in engine.get_execution_options()
        # This path is intentionally outside the declared cooperative boundary.
        with binding.maintenance(), engine.begin() as connection:
            connection.execute(text("UPDATE sample SET value='uncooperative'"))
    finally:
        engine.dispose()


def test_application_construction_cannot_create_artwork_during_maintenance(binding):
    gate = binding
    directory = settings(gate).metadata_artwork_directory
    assert not directory.exists()
    with gate.maintenance(), pytest.raises(api().MaintenanceBusy):
        create_app(settings(gate), maintenance=gate)
    assert not directory.exists()


def test_direct_artwork_writer_is_refused_even_with_readonly_session(binding):
    from app.metadata.artwork import cache_cover
    gate = binding
    engine = make_engine(f'sqlite:///{gate.database_path}', maintenance=gate)
    try:
        with gate.maintenance(), make_session_factory(engine)(readonly=True) as session:
            with pytest.raises(api().MaintenanceBusy):
                cache_cover(session=session, catalog_title_id=1, provider='anilist', external_id='1',
                    remote_url='https://invalid.example/cover.png', root=gate.library_root / 'artwork')
        assert not (gate.library_root / 'artwork').exists()
    finally:
        engine.dispose()


@pytest.mark.parametrize('kind', ['parent_escape', 'symlink'])
def test_optin_artwork_root_cannot_escape_scratch_binding(binding, kind):
    from dataclasses import replace
    gate = binding
    if kind == 'parent_escape':
        root = gate.database_path.parent / '..' / 'unrelated-artwork'
    else:
        root = gate.database_path.parent / 'artwork-alias'
        root.symlink_to(gate.library_root, target_is_directory=True)
    with pytest.raises(api().MaintenanceStopped):
        create_app(replace(settings(gate), metadata_artwork_directory=root), maintenance=gate)
