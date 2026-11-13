"""M2: approved coordinator inputs, private worker binds and pinned runtime IO."""
from dataclasses import replace
import hashlib
import json
import stat
import sys
from types import SimpleNamespace

import pytest

from test_reorganization_coordinator import (
    request,
    save,
    setup as legacy_setup,
    status,
)


@pytest.fixture
def production(legacy_setup, tmp_path, monkeypatch):
    from deploy.nexus import reorganization_deployment as release

    m, original, legacy, calls, source, approved, manifest = legacy_setup
    src = tmp_path / 'projects/anime-db/src'
    edge = src / 'deploy/nexus/service-edge/compose.edge.yaml'
    edge.parent.mkdir(parents=True)
    (src / '.git').mkdir()
    (src / '.git/HEAD').write_text('0123456789ab' + 'a' * 28 + '\n')
    stack = tmp_path / 'stack'
    stack.mkdir()
    files = (stack / 'compose.yaml', edge, stack / 'compose.v6.yaml')
    # Convert BEFORE constructing the instance exercised by every test.
    c = replace(original, compose_files=files, approved_root=tmp_path / 'approved-v6')
    generation = c.approved_root / ('generation-' + '0' * 24)
    (generation / 'binds').mkdir(parents=True)
    copies = tuple(generation / f'compose.{i}.yaml' for i in (1, 2, 3))
    bindings = []
    for i, (live, derived, runtime, target) in enumerate(zip(
            files, copies, (c.database_dir, c.service_status, c.state_dir),
            ('/data', '/public-status', '/state'))):
        anchor = generation / 'binds' / f'bind-{i}'
        anchor.mkdir(mode=0o700)
        info = runtime.stat()
        bindings.append(dict(source=str(runtime), target=str(anchor),
                             dev=info.st_dev, ino=info.st_ino))
        raw = ('services:\n  app:\n    volumes:\n      - type: bind\n'
               f'        source: {runtime}\n        target: {target}\n'
               '        bind:\n          create_host_path: false\n')
        live.write_text(raw)
        # V2 copies contain private aliases; comparing their bytes to live
        # inputs instead of source_sha256 must fail the positive cases.
        derived.write_text(raw.replace(str(runtime), str(anchor)))
    env = stack / '.env'
    env.write_text('ANIMEDB_UID=1000\nANIMEDB_GID=1000\nANIMEDB_CONTROL_GID=2001\n')
    (generation / '.env').write_bytes(env.read_bytes())
    metadata = dict(version=2, sources=[str(p) for p in files], env_source=str(env),
        project_directory=str(stack), project_name='anime-db',
        source_sha256=[hashlib.sha256(p.read_bytes()).hexdigest() for p in files],
        files=[dict(name=p.name, sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in copies],
        environment=dict(name='.env', sha256=hashlib.sha256(env.read_bytes()).hexdigest()),
        bindings=bindings)
    save(generation / 'deployment.json', metadata)
    deployment = release.Deployment(copies, generation / '.env', stack, 'anime-db', tuple(bindings))
    events, bound = [], []

    def resolve(selected, *, approved_root):
        assert selected == files
        assert approved_root == c.approved_root
        events.append('resolve')
        return deployment

    real_verify = release.verify_active_release

    def verify(selected, live, image):
        assert selected is deployment
        events.append('verify')
        return real_verify(selected, live, image)

    def bind(rows):
        # Kernel bind behavior has separate root coverage. Keep its exact
        # coordinator arguments and its position relative to Docker visible.
        events.append('bind')
        bound.extend(dict(row) for row in rows)

    def run(argv, **kwargs):
        events.append('run')
        return legacy.run(argv, **kwargs)

    monkeypatch.setattr(m, 'resolve_deployment', resolve)
    monkeypatch.setattr(m, 'ensure_bindings', bind)
    monkeypatch.setattr(release, 'verify_active_release', verify)
    monkeypatch.setitem(sys.modules, 'reorganization_deployment', release)
    host = m.Coordinator(c, run=run, healthy=lambda url: True)
    try:
        yield SimpleNamespace(m=m, c=c, host=host, calls=calls, events=events, bound=bound,
            source=source, approved=approved, manifest=manifest, src=src, env=env,
            deployment=deployment, metadata=metadata, release=release)
    finally:
        for coordinator in (host, legacy):
            coordinator.end_heartbeat()
            for runtime in coordinator._runtime.values():
                runtime.close()


def test_worker_uses_all_seven_private_anchors_and_keeps_trusted_sources(production):
    p = production
    c = p.c
    p.host.worker('check-start', p.source, 'start')
    assert len(p.calls) == 1
    argv = p.calls[0][0]
    assert argv[:2] == ['/usr/bin/docker', 'run']
    mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == '--mount']
    expected = [
        ('database_dir', '/data', False), ('state_dir', '/state', False),
        ('inbox', '/approval-inbox', False), ('outbox', '/approval-outbox', True),
        ('ledger', '/approval-ledger', False), ('backups', '/backups', False),
        ('service_status', '/service-status', False),
    ]
    root = c.private_dir / 'worker-binds'
    for name, inside, readonly in expected:
        assert (f'type=bind,src={root / name},dst={inside}' +
                (',readonly' if readonly else '')) in mounts
        assert not any(f'src={getattr(c, name)},' in mount for mount in mounts)
    assert f'type=bind,src={c.media_rw},dst=/media/anime' in mounts
    assert f'type=bind,src={c.worker_config},dst=/coordinator-config/worker.json,readonly' in mounts
    assert len(mounts) == 9
    assert len(p.bound) == 7
    for row, (name, _, _) in zip(p.bound, expected):
        info = getattr(c, name).stat()
        assert row == dict(source=str(getattr(c, name)), target=str(root / name),
                           dev=info.st_dev, ino=info.st_ino)
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o700
    assert p.events == ['bind'] * 7 + ['run']


def test_compose_resolves_then_verifies_v2_sources_then_binds_before_dispatch(production):
    p = production
    assert p.c.approved_root is not None
    assert p.metadata['version'] == 2
    for live, derived, digest in zip(p.c.compose_files, p.deployment.files, p.metadata['source_sha256']):
        assert live.read_bytes() != derived.read_bytes()
        assert hashlib.sha256(live.read_bytes()).hexdigest() == digest
        assert hashlib.sha256(derived.read_bytes()).hexdigest() != digest

    p.host.verify_image()

    assert p.events == ['resolve', 'verify', 'bind', 'run']
    assert p.bound == list(p.deployment.bindings)
    argv = p.calls[0][0]
    assert argv[:2] == ['/usr/bin/docker', 'compose']
    assert argv[argv.index('--project-directory') + 1] == str(p.c.compose_files[0].parent)
    assert argv[argv.index('--env-file') + 1] == str(p.deployment.env_file)
    assert argv[argv.index('-p') + 1] == 'anime-db'
    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == '-f'] == [str(f) for f in p.deployment.files]
    assert argv[-3:] == ['config', '--images', 'app']


@pytest.mark.parametrize('changed', ['release', 'image', 'base', 'edge', 'override', 'env'])
def test_release_mismatch_stops_before_any_binding_or_compose_and_is_not_retried(production, changed):
    p = production
    c = p.c
    p.release.verify_active_release(p.deployment, c.compose_files, c.image)
    p.events.clear()
    approved_bytes = [path.read_bytes() for path in p.deployment.files]
    if changed == 'release':
        (p.src / '.git/HEAD').write_text('f' * 40 + '\n')
    elif changed == 'image':
        p.host.config = replace(c, image='anime-db:v6-ffffffffffff')
    elif changed == 'env':
        p.env.write_text('ANIMEDB_UID=2000\nANIMEDB_GID=1000\nANIMEDB_CONTROL_GID=2001\n')
    else:
        live = c.compose_files[('base', 'edge', 'override').index(changed)]
        live.write_bytes(live.read_bytes() + b'\n# unapproved live edit\n')
    request(c, 'start', p.source, p.approved, p.manifest)

    p.host.tick()

    stopped = status(c, p.source, 'start')
    assert stopped['state'] == 'STOP' and stopped['e7_started'] is False
    private = p.host.read_json(c.private_dir / f'{p.source}.start-error.json')
    assert private['error'] == 'ValueError'
    expected = ('approved image differs from active src release' if changed in ('release', 'image')
                else 'active deployment environment differs from approval' if changed == 'env'
                else 'active Compose sources differ from approved generation')
    assert expected in private['detail']
    assert p.events == ['resolve', 'verify']
    assert p.bound == [] and p.calls == []
    assert not (c.private_dir / 'worker-binds').exists()
    assert list(c.ledger.iterdir()) == []
    assert [path.read_bytes() for path in p.deployment.files] == approved_bytes
    p.host.tick()
    assert p.events == ['resolve', 'verify']
    assert p.bound == [] and p.calls == []


@pytest.mark.parametrize('kind, outcome, verbs', [
    ('prepare', 'READY', ['plan', 'publish']),
    ('start', 'NORMAL', ['execute']),
])
def test_production_status_progress_and_ledger_use_real_runtime_publication(
        production, monkeypatch, kind, outcome, verbs):
    p = production
    c = p.c
    writes = []
    real_write = p.m.RuntimeDirectory.write
    private_write = p.m.write_json

    def runtime_write(runtime, name, value, **kwargs):
        writes.append((runtime.path, name))
        return real_write(runtime, name, value, **kwargs)

    def private_only(path, value, **kwargs):
        assert path.parent not in p.host._runtime, 'runtime publication escaped RuntimeDirectory'
        return private_write(path, value, **kwargs)

    monkeypatch.setattr(p.m.RuntimeDirectory, 'write', runtime_write)
    monkeypatch.setattr(p.m, 'write_json', private_only)
    request_id = 'a' * 64 if kind == 'prepare' else p.source
    request(c, kind, request_id, p.approved, p.manifest)

    p.host.tick()

    assert status(c, request_id, kind)['state'] == outcome
    progress = p.host.read_json(c.service_status / 'progress.json')
    assert progress['app_ready'] is True and progress['active'] is False
    assert progress['source_sha256'] == p.source
    assert (c.inbox, f'{request_id}.{kind}-status.json') in writes
    assert (c.service_status, 'progress.json') in writes
    for verb in verbs:
        name = f'{request_id}.{verb}-runtime.json'
        assert (c.ledger, name) in writes
        proof = p.host.read_json(c.ledger / name)
        assert proof['scanner_paused_through_verification'] is True
        assert proof['inventory_writer_paused_through_verification'] is True
        assert proof['mount_read_write_checked'] is True
        if verb == 'publish':
            assert proof['snapshot_capable'] is True
            assert proof['snapshot_id'] == 'snapshot-1'
            assert proof['snapshot_manifest_id'] == p.manifest
    for path, mode in ((c.inbox / f'{request_id}.{kind}-status.json', 0o600),
                       (c.service_status / 'progress.json', 0o644),
                       *((c.ledger / f'{request_id}.{verb}-runtime.json', 0o600) for verb in verbs)):
        info = path.stat()
        assert (info.st_uid, info.st_gid) == (c.uid, c.gid)
        assert stat.S_IMODE(info.st_mode) == mode


@pytest.mark.parametrize('directory', ['inbox', 'service_status'])
def test_runtime_replaced_between_tick_and_worker_stops_without_further_dispatch(
        production, tmp_path, directory):
    p = production
    c = p.c
    request(c, 'start', p.source, p.approved, p.manifest)
    original_run = p.host.run
    old = tmp_path / f'{directory}-original'
    replaced = getattr(c, directory)
    boundary = []

    def swap_after_image_check(argv, **kwargs):
        result = original_run(argv, **kwargs)
        assert argv[-3:] == ['config', '--images', 'app']
        replaced.rename(old)
        replaced.mkdir(mode=0o700)
        boundary.append((list(p.calls), list(p.bound), list(p.events)))
        return result

    p.host.run = swap_after_image_check
    p.host.tick()

    inbox = old if directory == 'inbox' else c.inbox
    service = old if directory == 'service_status' else c.service_status
    stopped = json.loads((inbox / f'{p.source}.start-status.json').read_bytes())
    assert stopped['state'] == 'STOP' and stopped['e7_started'] is False
    assert json.loads((service / 'progress.json').read_bytes())['outcome'] == 'STOP'
    assert list(replaced.iterdir()) == []
    assert len(boundary) == 1
    assert (p.calls, p.bound, p.events) == boundary[0]
    assert not (c.private_dir / 'worker-binds').exists()
    assert list(c.ledger.iterdir()) == []
    error = p.host.read_json(c.private_dir / f'{p.source}.start-error.json')
    assert error['error'] == 'ValueError' and 'runtime directory replaced' in error['detail']
    assert p.host.read_json(c.private_dir / f'{p.source}.start-done.json')['state'] == 'STOP'
    with pytest.raises(ValueError, match='runtime directory replaced'):
        p.host.tick()
    assert (p.calls, p.bound, p.events) == boundary[0]
