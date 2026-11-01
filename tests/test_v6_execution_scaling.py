"""Local E7/E7H/NORMAL scaling; synthetic mount policy is not a CIFS benchmark.

V6_SCALING_METRICS_PATH stores machine-readable per-case measurements. Increase
V6_SCALING_FILE_BYTES for a separately requested larger local workload. Timing
is reported, not asserted: full media hashing and retained descriptors are the
regressions this test bounds independently of machine speed.
Hash counters cover app._hash_file only; they exclude other DB/backup digest
readers and do not measure physical storage I/O.
"""
from collections import Counter
from dataclasses import replace
import gc
import hashlib
import json
import os
import resource
from pathlib import Path
import shutil
import sys
import time

import pytest
from sqlalchemy.orm import Session

from app import target_execution_identity as identity
from app import target_execution_mounts as mounts
from app.database import make_engine
from app.models import Video
from app.target_execution_cleanup import approve_cleanup
from app.target_execution_manifest import manifest_to_json
from test_v6_execution_file_actions import generate
from test_v6_execution_projection import expected_rows
from test_v6_p2c_rescan import approve
from test_v6_production import SHARE, production
from v6_database_case import database_rows


def _prepare(owner, path, count, directory_mode, file_bytes):
    """Retain the production fixture's primary and confirmed quarantine action."""
    # The production fixture also has a root inventory text KEEP action; this
    # workload varies the exact number of equally sized physical video actions.
    (owner.root / 'seznam-souboru.txt').unlink()
    engine = make_engine(f'sqlite:///{path}')
    content = b'v' * file_bytes
    try:
        with Session(engine) as db:
            primary = db.get(Video, 100)
            for number in range(2, count):
                parent = 'old/copy' if directory_mode == 'constant' else f'old/branch-{number:03d}'
                source = owner.root / parent / f'video-{number:03d}.mkv'
                source.parent.mkdir(mode=0o700, exist_ok=True)
                source.write_bytes(content)
                source.chmod(0o600)
                info = source.stat()
                db.add(Video(id=300 + number, catalog_title_id=primary.catalog_title_id,
                    catalog_collection_id=primary.catalog_collection_id,
                    relative_path=source.relative_to(owner.root).as_posix(), root_folder='old',
                    filename=f'Historical release - {number:02d}.mkv', size=info.st_size,
                    mtime_ns=info.st_mtime_ns, file_type='episode', content_type_manual='episode',
                    season_episode_number=number, local_episode_number=number,
                    episode_number_manual_override=number,
                    episode_number_verified_at=primary.episode_number_verified_at,
                    manual_hardsub_cs=True, manual_hardsub_verified_at=primary.episode_number_verified_at,
                    czsk_availability_manual='missing'))
            db.flush()
            for video in db.query(Video):
                source = owner.root / video.relative_path
                source.write_bytes(content)
                source.chmod(0o600)
                video.size, video.mtime_ns = source.stat().st_size, source.stat().st_mtime_ns
            from app.hierarchy_evaluation import finalize_collection_hierarchy
            finalize_collection_hierarchy(primary.catalog_collection,
                list(primary.catalog_collection.videos), preserved_title_ids={primary.catalog_title_id})
            db.commit()
    finally:
        engine.dispose()
    manifest = approve(generate(owner, path))
    assert not manifest.payload.generation_diagnostics
    assert len(manifest.payload.actions) == count
    assert sum(a.action == 'QUARANTINE' for a in manifest.payload.actions) == 1
    return manifest


class _Measurements:
    """Observe real full hashes and FD counts without replacing their results."""

    def __init__(self, root, database):
        self.root, self.database = root, database
        self.calls, self.bytes = Counter(), Counter()
        self.phase = 'execution_cleanup_normal'
        self.start_fds = len(os.listdir('/proc/self/fd'))
        self.peak_fds = self.start_fds
        self.hash_seconds = 0.0
        self.video_read_bytes = 0

    def marker(self, phase):
        os.write(2, f'V6_MEDIA_PHASE {phase} {self.root}\n'.encode())

    def sample_fds(self):
        # Each absolute count includes the temporary /proc iterator descriptor;
        # its constant cost cancels out of the reported/checked FD delta.
        self.peak_fds = max(self.peak_fds, len(os.listdir('/proc/self/fd')))

    def install(self, monkeypatch):
        real_hash, real_open, real_dup = identity._hash_file, os.open, os.dup
        real_read, real_pread = os.read, os.pread

        def payload(fd, count):
            if self.phase == 'fixture_view_copy':
                return
            try:
                path = Path(os.readlink(f'/proc/self/fd/{fd}'))
            except OSError:
                return
            if path.is_relative_to(self.root) and path.suffix.lower() in {'.mkv', '.mp4', '.avi', '.m4v'}:
                self.video_read_bytes += count

        def read(fd, size):
            value = real_read(fd, size)
            payload(fd, len(value))
            return value

        def pread(fd, size, offset):
            value = real_pread(fd, size, offset)
            payload(fd, len(value))
            return value

        def hashed(fd):
            self.sample_fds()
            target = Path(os.readlink(f'/proc/self/fd/{fd}'))
            kind = 'media' if target.is_relative_to(self.root) else 'database' if target == self.database else 'other'
            started = time.perf_counter()
            observed, content = real_hash(fd)
            self.hash_seconds += time.perf_counter() - started
            self.calls[f'{self.phase}:{kind}'] += 1
            self.bytes[f'{self.phase}:{kind}'] += content.bytes_hashed
            return observed, content

        def opened(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            self.sample_fds()
            return fd

        def duplicated(*args, **kwargs):
            fd = real_dup(*args, **kwargs)
            self.sample_fds()
            return fd

        # E7 modules include direct imports; patch every already-loaded alias
        # of this exact function so the counter covers the complete lifecycle.
        for module in tuple(sys.modules.values()):
            if module is not None and getattr(module, '__name__', '').startswith('app.'):
                if getattr(module, '_hash_file', None) is real_hash:
                    monkeypatch.setattr(module, '_hash_file', hashed)
        monkeypatch.setattr(os, 'open', opened)
        monkeypatch.setattr(os, 'dup', duplicated)
        monkeypatch.setattr(os, 'read', read)
        monkeypatch.setattr(os, 'pread', pread)


def _save(metrics):
    print('V6_SCALING ' + json.dumps(metrics, sort_keys=True), flush=True)
    destination = os.environ.get('V6_SCALING_METRICS_PATH')
    if destination:
        path = Path(destination)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        previous = json.loads(path.read_text()) if path.exists() else []
        key = (metrics['actions'], metrics['directory_mode'], metrics['file_bytes'])
        previous = [m for m in previous if (m['actions'], m['directory_mode'], m['file_bytes']) != key]
        path.write_text(json.dumps([*previous, metrics], indent=2, sort_keys=True) + '\n')


@pytest.mark.parametrize('directory_mode', ['constant', 'growing'])
@pytest.mark.parametrize('count', [int(value) for value in os.environ.get('V6_SCALING_COUNTS', '2,4,8,16').split(',')])
def test_full_execution_hash_and_fd_cost_remains_bounded(tmp_path, monkeypatch, count, directory_mode):
    """Reintroducing full hashes of every completed file at each gate must fail."""
    file_bytes = int(os.environ.get('V6_SCALING_FILE_BYTES', 64 * 1024))
    assert file_bytes > 0
    fixture = production.__wrapped__(tmp_path, monkeypatch)
    owner, path, _, runtime = next(fixture)
    try:
        manifest = _prepare(owner, path, count, directory_mode, file_bytes)
        backup = Path(runtime.db_backup_path)
        backup.write_bytes(path.read_bytes())
        fingerprint = manifest.payload.db_fingerprint
        runtime = replace(runtime, snapshot_at=manifest.created_at,
            snapshot_manifest_id=manifest.manifest_id, db_backup_sha256=fingerprint.sha256,
            db_backup_size=fingerprint.size, db_backup_user_version=fingerprint.user_version,
            db_backup_manifest_id=manifest.manifest_id)
        original = database_rows(path)
        root = owner.root
        original_directories = sum(p.is_dir() for p in root.rglob('*'))
        lock = path.parent / 'runtime.lock'
        lock.write_bytes(b'')
        lock.chmod(0o600)
        owner.close()
        gc.collect()
        measurement = _Measurements(root, path)
        selected = []
        metrics = dict(actions=count, directory_mode=directory_mode, file_bytes=file_bytes,
            original_directories=original_directories, execution_seconds=None,
            ro_startup_seconds=None, start_open_fds=measurement.start_fds,
            app_source=str(Path(identity.__file__).resolve().parent))

        def record(stage):
            metrics.update(stage=stage, removed_directories=len(selected),
                full_hash_calls=dict(measurement.calls), full_hash_bytes=dict(measurement.bytes),
                full_hash_seconds=measurement.hash_seconds,
                peak_sampled_open_fds=measurement.peak_fds,
                peak_sampled_fd_delta=measurement.peak_fds-measurement.start_fds,
                video_read_bytes_python=measurement.video_read_bytes,
                process_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                end_open_fds=len(os.listdir('/proc/self/fd')))
            _save(metrics)

        def decide(proposal):
            selected.extend(p.path for p in proposal.candidates if p.path == 'old' or p.path.startswith('old/'))
            return approve_cleanup(proposal, tuple(selected), actor='synthetic operator', reason='scaling fixture cleanup')

        from app.tools.reorganize import execute_approved_reorganization
        with monkeypatch.context() as instrument:
            measurement.install(instrument)
            measurement.marker('EXECUTION')
            started = time.perf_counter()
            result = execute_approved_reorganization(database=path, library_root=root, share=SHARE,
                share_root='/', manifest=manifest,
                approved_sha256=hashlib.sha256(manifest_to_json(manifest).encode()).hexdigest(),
                runtime=runtime, runtime_lock=lock, cleanup_decision=decide)
            metrics['execution_seconds'] = time.perf_counter() - started
            record('NORMAL')
            measurement.marker('FIXTURE_VIEW_COPY')
            measurement.phase = 'fixture_view_copy'
            assert result['state'] == 'NORMAL'
            assert selected and all(not (root / p).exists() for p in selected)
            assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
            # Independent local view models a different inode namespace, while
            # only the CIFS policy boundary is substituted by the fixture.
            rw_view = root.with_name('completed-rw-view')
            root.rename(rw_view)
            shutil.copytree(rw_view, root, copy_function=shutil.copy2)
            observe = mounts._check_production_mount

            def readonly(root_path, fd, records, *, share, share_root, writable):
                if writable:
                    raise ValueError('RO application cannot obtain RW execution authority')
                return replace(observe(root_path, fd, records, share=share,
                    share_root=share_root, writable=False), options=('ro',))

            instrument.setattr(mounts, '_check_production_mount', readonly)
            measurement.phase = 'ro_startup'
            measurement.marker('RO_STARTUP')
            from fastapi.testclient import TestClient
            from app.main import create_app
            from test_v6_normal_operation import settings_for
            started = time.perf_counter()
            with TestClient(create_app(settings_for(path, root))) as client:
                assert client.get('/health').status_code == 200
            metrics['ro_startup_seconds'] = time.perf_counter() - started
            record('RO_STARTED')
            measurement.marker('END')
        assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
        # Whole-state gates verify every metadata entry. Ordinary rename must
        # never read video payload, including at those final gates.
        media_calls = sum(value for key, value in measurement.calls.items() if key.endswith(':media'))
        assert media_calls == 0, metrics
        assert measurement.video_read_bytes == 0, metrics
        # These cases have fixed directory depth. Completed file descriptors
        # and one descriptor per sibling directory must not accumulate.
        assert measurement.peak_fds - measurement.start_fds <= 64, metrics
        assert metrics['end_open_fds'] <= measurement.start_fds, metrics
    finally:
        fixture.close()
