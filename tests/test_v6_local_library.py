"""Persistent local E7 integration; artifacts intentionally remain for inspection."""
from pathlib import Path
import tempfile
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import make_engine
from app.models import Video
from app.scanner import LibrarySafetyError
from app.target_execution_guard import NormalOperation, _require_no_evidence
from app.main import create_app
from app.probe import probe_video
from app.target_execution_manifest import manifest_to_json
from test_scanner_reorganization import case, execute, scan
from test_v6_execution_file_actions import generate
from test_v6_p2c_rescan import approve
from test_v6_normal_operation import complete, settings_for, authority
from v6_database_case import database_rows


@pytest.fixture
def tmp_path():
    # Retained outside pytest's rotating /tmp trees, adjacent to the test DB.
    configured = os.environ.get('ANIMEDB_LOCAL_TEST_PARENT')
    if not configured:
        pytest.skip('set ANIMEDB_LOCAL_TEST_PARENT to a persistent local directory outside the repository')
    parent = Path(configured)
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix='local-', dir=parent))


def test_large_local_library_source_target_sqlite_normal_restart(case, monkeypatch):
    owner, path, _ = case
    assert not owner.root.is_relative_to('/tmp')
    source = next(owner.root.rglob('*.mkv'))
    # Write real blocks rather than allocating a large bytes object or sparse file.
    with source.open('ab') as stream:
        for _ in range(33):
            stream.write(b'local media block' * 65536)
    info = source.stat()
    assert info.st_size > 8 * 1024 * 1024
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as db:
            video = db.query(Video).filter_by(relative_path=source.relative_to(owner.root).as_posix()).one()
            video.size, video.mtime_ns = info.st_size, info.st_mtime_ns
            ident = video.id
            db.commit()
    finally:
        engine.dispose()
    # Settle the fixture's derived numbering after its explicit manual edits,
    # using the ordinary scanner before sealing the SOURCE projection.
    assert scan(path, owner.root).unchanged == 3
    manifest = approve(generate(owner, path))
    path.with_name(path.stem + '.manifest.json').write_text(manifest_to_json(manifest))
    before = authority(path)
    # Fixture indexing uses synthetic probe data; post-execution scans use the
    # real scanner/probe binding (unchanged files need no technical re-probe).
    monkeypatch.setattr('app.scanner.service.probe_video', probe_video)
    execute((owner, path, manifest), resume_scanning=True)
    action = next(a for a in manifest.payload.actions if a.object_kind == 'video' and a.object_id == ident)
    target = owner.root / action.target
    assert not source.exists()
    assert (target.stat().st_dev, target.stat().st_ino, target.stat().st_size) == (info.st_dev, info.st_ino, info.st_size)
    rows, raw = database_rows(path), path.read_bytes()
    # SCANNING must never enter the ordinary write lifecycle.
    with monkeypatch.context() as checks:
        def no_write_scan(*args, **kwargs):
            pytest.fail('SCANNING entered the ordinary write lifecycle')
        checks.setattr('app.scanner.service._scan_library', no_write_scan)
        assert scan(path, owner.root).unchanged == 3
    assert database_rows(path) == rows and path.read_bytes() == raw
    alias = path.with_name('alias.sqlite')
    os.link(path, alias)
    try:
        with pytest.raises(LibrarySafetyError):
            scan(alias, owner.root)
    finally:
        alias.unlink()
    complete(path, owner.root)
    assert isinstance(_require_no_evidence(path), NormalOperation)
    root = owner.root
    artifacts = (owner.journal_path, owner.directory_evidence_path, *owner.evidence_paths)
    history = {p: p.read_bytes() for p in artifacts}
    owner.close()
    with TestClient(create_app(settings_for(path, root))) as client:
        assert client.get('/health').status_code == 200
        response = client.post('/scan', follow_redirects=False)
        assert response.status_code == 303 and 'error=' not in response.headers['location']
    assert scan(path, root).unchanged == 3
    assert database_rows(path) == rows
    assert authority(path) == before
    assert all(p.read_bytes() == raw for p, raw in history.items())
    assert isinstance(_require_no_evidence(path), NormalOperation)
