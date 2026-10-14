"""Small real SQLite/library fixture for the single-video E5A contract."""
from pathlib import Path
import sqlite3

from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app.models import Video, ExternalSubtitle, ExternalSubtitleCompatibility
from app.target_execution_manifest import build_execution_manifest
from app.target_planner import project_targets
from app.target_planner_filesystem import inventory_filesystem, plan_library
from app.target_planner_service import load_planner_context, readonly_planner_session
from app.tools.target_plan import database_fingerprint
from test_physical_naming import collection, title, attach_metadata, NOW
from test_v6_execution_filesystem import seal


def database_rows(path):
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        return {table.name: tuple(connection.execute(
            'SELECT ' + ','.join('"' + c.name + '"' for c in table.columns) +
            ' FROM "' + table.name + '" ORDER BY ' +
            ','.join('"' + c.name + '"' for c in table.primary_key.columns)).fetchall())
            for table in Base.metadata.sorted_tables}
    finally:
        connection.close()


def prepare_database(session):
    path = session.root.parent / (session.root.name + '.sqlite')
    source = session.root / 'old/source.mkv'
    source.parent.mkdir(mode=0o700)
    source.write_bytes(b'original scratch video')
    source.chmod(0o600)
    info = source.stat()
    engine = make_engine(f'sqlite:///{path}')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            owner = collection(db)
            main = title(db, owner, metadata=False)
            main.relative_root_path = 'Show/Season 01'
            attach_metadata(main, str(main.id), 'Show')
            moving = Video(id=100, catalog_title=main, catalog_collection=owner,
                relative_path='old/source.mkv', root_folder='old', filename='Historical release - 01.mkv',
                size=info.st_size, mtime_ns=info.st_mtime_ns, file_type='episode',
                season_episode_number=1, local_episode_number=1,
                episode_number_manual_override=1, episode_number_verified_at=NOW,
                content_type_manual='episode', manual_hardsub_cs=True,
                manual_hardsub_verified_at=NOW, czsk_availability_manual='missing')
            other = Video(id=200, catalog_title=main, catalog_collection=owner,
                relative_path='temporary/second.mkv', root_folder='temporary', filename='Original second - 02.mkv',
                size=5, mtime_ns=1, file_type='episode', season_episode_number=2,
                local_episode_number=2, content_type_manual='episode')
            subtitle = ExternalSubtitle(relative_path='temporary/second.cs.ass', codec='ass')
            db.add_all([moving, other, subtitle])
            db.flush()
            db.add(ExternalSubtitleCompatibility(external_subtitle=subtitle, video=other,
                status='confirmed_compatible', match_method='manual', verified_at=NOW))
            db.commit()
            targets = project_targets(load_planner_context(db))
            for row in targets:
                if row.object_kind == 'video' and row.object_id == moving.id:
                    continue
                asset = session.root / row.target_relative_path
                asset.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                asset.write_bytes(b'other' if row.object_kind == 'video' else b'subtitle')
                asset.chmod(0o600)
                if row.object_kind == 'video':
                    other.relative_path = row.target_relative_path
                    other.root_folder = Path(row.target_relative_path).parts[0]
                    other.mtime_ns = asset.stat().st_mtime_ns
                else:
                    subtitle.relative_path = row.target_relative_path
            db.commit()
    finally:
        engine.dispose()
    path.chmod(0o600)
    with readonly_planner_session(path) as db:
        context = load_planner_context(db)
    # Target parents are caller-prepared scratch data, never executor mkdirs.
    projected = next(r for r in project_targets(context) if r.object_kind == 'video' and r.object_id == 100)
    target = session.root / projected.target_relative_path
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in session.root.rglob('*'):
        if directory.is_dir():
            directory.chmod(0o700)
    snapshot = inventory_filesystem(session.root)
    plan = plan_library(context, snapshot)
    manifest = build_execution_manifest(context, snapshot, plan, database_fingerprint(path),
        created_at='2026-10-14T12:00:00Z', mount_identity=session.mount_identity)
    assert not manifest.payload.generation_diagnostics, manifest.payload.generation_diagnostics
    assert manifest.payload.expected_post_hash is not None
    actions = tuple(a for a in manifest.payload.actions if a.action == 'MOVE')
    assert len(actions) == 1 and actions[0].object_id == 100
    assert {(p.object_kind, p.object_id, p.field) for p in manifest.payload.locator_patches} == {
        ('video', 100, 'relative_path'), ('video', 100, 'root_folder')}
    # Explicitly approved synthetic one-action subset; all other assets are KEEP.
    manifest = seal(manifest, actions)
    return manifest, source, target, path
