"""Real root/non-root boundary tests; disposable /run tree only, no services."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import pytest


def test_lab_three_layers_stage_v2_with_runtime_anchors(tree):
    """Exercise the actual LAB bind grammar, including its app entrypoint mount."""
    from deploy.nexus import reorganization_deployment as d
    repo = Path(__file__).resolve().parents[1]
    lab = tree / 'lab'
    lab.mkdir()
    (lab / 'appdata/approval').mkdir(parents=True)
    for directory in (lab / 'appdata', lab / 'appdata/approval'):
        os.chown(directory, 1000, 1000)
    files = []
    for name in ('compose.yaml', 'compose.edge.yaml', 'compose.v6.yaml'):
        raw = (repo / 'deploy/lab' / name).read_text().replace('/srv/animedb-v6-lab', str(lab))
        raw = raw.replace('/run/animedb-v6-lab', str(lab / 'control'))
        for _, source in d._bind_sources(raw):
            source = Path(source)
            if source.suffix == '.py':
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('# LAB entrypoint\n')
            else:
                source.mkdir(parents=True, exist_ok=True)
        path = lab / name
        path.write_text(raw)
        files.append(path)
    env = lab / '.env'
    env.write_text('ANIMEDB_UID=1000\nANIMEDB_GID=1000\nANIMEDB_CONTROL_GID=2001\n')
    generation = d.stage_deployment(files, env_file=env, project_name='animedb-v6-lab',
        approved_root=tree / 'approved')
    d.approve_deployment(generation, reviewed_sha256=d.generation_sha256(generation))
    selected = d.resolve_deployment(files, approved_root=tree / 'approved')
    metadata = json.loads((generation / 'deployment.json').read_text())
    assert metadata['version'] == 2 and selected.project_name == 'animedb-v6-lab'
    assert metadata['source_sha256'] == [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]
    assert len(selected.bindings) == 7
    assert all(str(lab / 'appdata') in row['source'] for row in selected.bindings)
sys.path.insert(0, str(Path(__file__).parents[1]))
from deploy.nexus import reorganization_coordinator as c
from deploy.nexus import reorganization_deployment as d

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason='isolated root privilege test; run explicitly with sudo')


@pytest.fixture
def tree():
    with tempfile.TemporaryDirectory(prefix='animedb-host-test-',dir='/run') as name:
        yield Path(name)


def test_wrong_root_private_same_filesystem_anchor_is_refused(tree):
    """A bind mount must be detected even when st_dev matches its parent."""
    source, other, target = (tree / name for name in ('expected', 'wrong', 'anchor'))
    for path in (source, other, target):
        path.mkdir(mode=0o700)
    info, wrong = source.stat(), other.stat()
    row = dict(source=str(source), target=str(target), dev=info.st_dev, ino=info.st_ino)
    d.ensure_bindings([dict(source=str(other), target=str(target), dev=wrong.st_dev, ino=wrong.st_ino)])
    try:
        assert target.stat().st_dev == target.parent.stat().st_dev
        with pytest.raises(ValueError, match='unexpected existing bind anchor'):
            d.ensure_bindings([row])
    finally:
        # The regression used to stack a second mount; release both in this
        # disposable namespace before TemporaryDirectory removes the tree.
        while any(line.split()[4] == str(target) for line in Path('/proc/self/mountinfo').read_text().splitlines()):
            if ctypes.CDLL(None, use_errno=True).umount2(os.fsencode(target), 0):
                raise OSError(ctypes.get_errno(), 'failed to release test mount')


def test_real_root_config_trust_and_user_owned_project(tree):
    trusted=tree/'etc';trusted.mkdir(mode=0o700)
    config=trusted/'config.json';config.write_text('{}');config.chmod(0o600)
    c.secure_root_path(config)
    config.chmod(0o666)
    with pytest.raises(ValueError):c.secure_root_path(config)
    config.chmod(0o600)
    alias=trusted/'alias';alias.symlink_to(config)
    with pytest.raises(ValueError):c.secure_root_path(alias)
    project=tree/'project';project.mkdir(mode=0o750);os.chown(project,65534,65534)
    files=tuple(project/name for name in ('base.yaml','edge.yaml','override.yaml'))
    for path in files:path.write_text('services:\n  app:\n    image: anime-db:v6-db40987\n')
    env=project/'.env';env.write_text('ANIMEDB_UID=65534\nANIMEDB_GID=65534\n')
    root=tree/'generations';root.mkdir(mode=0o700)
    generation=d.stage_deployment(files,env_file=env,approved_root=root)
    d.approve_deployment(generation,reviewed_sha256=d.generation_sha256(generation))
    assert d.resolve_deployment(files,approved_root=root).project_directory==project
    assert project.stat().st_uid==65534
    config.rename(project/'untrusted.json')
    with pytest.raises(ValueError):c.secure_root_path(project/'untrusted.json')


def test_real_root_publication_drops_identity_and_resists_target_symlink(tree):
    runtime=tree/'runtime';runtime.mkdir(mode=0o700);os.chown(runtime,65534,65534)
    pin=c.RuntimeDirectory(runtime,65534,65534)
    protected=tree/'protected';protected.write_text('root-only');protected.chmod(0o600)
    (runtime/'status.json').symlink_to(protected)
    pin.write('status.json',{'ok':True},mode=0o644)
    assert protected.read_text()=='root-only'
    assert (runtime/'status.json').stat().st_uid==65534
    pin.write('once.json',{'one':1},exclusive=True)
    with pytest.raises(FileExistsError):pin.write('once.json',{'two':2},exclusive=True)
    assert json.loads((runtime/'once.json').read_text())=={'one':1}
    # Child must not inherit root's supplementary groups or retained identity.
    from deploy.nexus import host_io
    program=host_io.WRITE_PROGRAM
    try:
        host_io.WRITE_PROGRAM="import os,sys; assert os.geteuid()==65534 and os.getegid()==65534 and os.getgroups()==[]; "+program
        pin.write('identity.json',{'checked':True})
    finally:
        host_io.WRITE_PROGRAM=program
        pin.close()


def test_socket_dedicated_group_allows_edge_and_rejects_unrelated_process(tree):
    # Disposable numeric identities, independent of /etc/group or _ssh.
    directory=tree/'socket';directory.mkdir(mode=0o750);os.chown(directory,0,2001)
    tree.chmod(0o755)  # This newly created test root only.
    path=directory/'control.sock'
    server=socket.socket(socket.AF_UNIX);server.bind(str(path));server.listen(2)
    os.chown(path,0,2001);os.chmod(path,0o660)
    script='import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])'
    try:
        allowed=subprocess.run(['/usr/bin/python3','-I','-c',script,str(path)],user=65534,group=65534,extra_groups=[2001],capture_output=True)
        assert allowed.returncode==0,allowed.stderr
        denied=subprocess.run(['/usr/bin/python3','-I','-c',script,str(path)],user=65534,group=65534,extra_groups=[],capture_output=True)
        assert denied.returncode!=0 and b'PermissionError' in denied.stderr
    finally:
        server.close()


def test_bind_anchor_uses_open_descriptor_if_source_path_is_swapped(tree,monkeypatch):
    import ctypes
    from deploy.nexus import host_io
    source=tree/'user/source';source.mkdir(parents=True)
    (source/'identity').write_text('original')
    other=tree/'other';other.mkdir();(other/'identity').write_text('unexpected')
    private=tree/'private';private.mkdir(mode=0o700)
    target=private/'anchor';target.mkdir(mode=0o700)
    info=source.stat()
    original=host_io.open_directory
    def swap(path):
        fd=original(path)
        if Path(path)==source:
            source.rename(source.with_name('saved'))
            source.symlink_to(other,target_is_directory=True)
        return fd
    monkeypatch.setattr(host_io,'open_directory',swap)
    try:
        host_io.ensure_bindings([dict(source=str(source),target=str(target),dev=info.st_dev,ino=info.st_ino)])
        assert (target/'identity').read_text()=='original'
    finally:
        ctypes.CDLL(None).umount2(os.fsencode(target),2)


def test_approved_generation_derives_pinned_bind_and_original_source_hash(tree):
    import ctypes,hashlib
    project=tree/'project';project.mkdir(mode=0o750);os.chown(project,65534,65534)
    runtime=project/'data';runtime.mkdir(mode=0o700);os.chown(runtime,65534,65534)
    (runtime/'same').write_text('same data')
    files=tuple(project/name for name in ('base.yaml','edge.yaml','override.yaml'))
    files[0].write_text(f'services:\n  app:\n    volumes:\n      - type: bind\n        source: {runtime}\n        target: /data\n        bind:\n          create_host_path: false\n')
    for path in files[1:]:path.write_text('services:\n  app:\n    image: anime-db:v6-db40987\n')
    env=project/'.env';env.write_text('ANIMEDB_UID=65534\nANIMEDB_GID=65534\n')
    root=tree/'approved';root.mkdir(mode=0o700)
    generation=d.stage_deployment(files,env_file=env,approved_root=root)
    d.approve_deployment(generation,reviewed_sha256=d.generation_sha256(generation))
    deployment=d.resolve_deployment(files,approved_root=root)
    metadata=json.loads((generation/'deployment.json').read_text())
    assert metadata['source_sha256'][0]==hashlib.sha256(files[0].read_bytes()).hexdigest()
    assert len(deployment.bindings)==1 and str(runtime) not in deployment.files[0].read_text()
    target=Path(deployment.bindings[0]['target'])
    try:
        d.ensure_bindings(deployment.bindings)
        assert (target/'same').read_text()=='same data'
        d.ensure_bindings(deployment.bindings)  # Idempotent; never stack a second bind.
        assert target.stat().st_ino==runtime.stat().st_ino
    finally:
        ctypes.CDLL(None).umount2(os.fsencode(target),2)


def test_load_config_accepts_user_owned_nexus_ancestors_but_keeps_trusted_roots(tree):
    repo=Path(__file__).parents[1]
    config=json.loads((repo/'deploy/nexus/config/reorganization-coordinator.json.example').read_text())
    config.update(image='anime-db:v6-db40987',uid=65534,gid=65534)
    nexus=tree/'nexus';nexus.mkdir(mode=0o750);os.chown(nexus,65534,65534)
    for name in c.RUNTIME_DIRECTORIES:
        directory=nexus/name;directory.mkdir(mode=0o700);os.chown(directory,65534,65534)
        config[name]=str(directory)
    for name in ('media_ro','media_rw','control_dir','private_dir'):
        directory=tree/name;directory.mkdir(mode=0o700);config[name]=str(directory)
    os.chown(config['control_dir'],0,2001)
    for name in ('worker_config','runtime_evidence'):
        path=tree/(name+'.json');config[name]=str(path)
    Path(config['worker_config']).write_text((repo/'deploy/nexus/config/reorganization-worker.json.example').read_text())
    Path(config['runtime_evidence']).write_text('{}')
    for name in ('snapshot_hook','writers_stop_hook','writers_start_hook'):
        config[name]=str(Path(sys.executable).resolve())  # Validate existing executable only; never invoke hook.
    files=tuple(tree/f'compose{n}.yaml' for n in (1,2,3))
    for path in files:path.write_text('services:\n  app:\n    image: anime-db:v6-db40987\n')
    env=tree/'.env';env.write_text('ANIMEDB_UID=65534\nANIMEDB_GID=65534\n')
    root=tree/'approved';root.mkdir(mode=0o700)
    generation=d.stage_deployment(files,env_file=env,approved_root=root)
    d.approve_deployment(generation,reviewed_sha256=d.generation_sha256(generation))
    config.update(compose_files=[str(path) for path in files],approved_root=str(root))
    record=tree/'coordinator.json';record.write_text(json.dumps(config));record.chmod(0o600)
    before=nexus.stat()
    loaded=c.load_config(record)
    assert loaded.database_dir==nexus/'database_dir'
    assert (nexus.stat().st_uid,nexus.stat().st_mode)==(before.st_uid,before.st_mode)
    nexus.chmod(0o770)
    with pytest.raises(ValueError):c.load_config(record)


def test_production_worker_real_anchors_and_nonroot_evidence(tree):
    # No Docker execution or NAS: real production branch, mounts and UID switch.
    runtime=tree/'nexus';runtime.mkdir(mode=0o750);os.chown(runtime,65534,65534)
    dirs={}
    for name in c.RUNTIME_DIRECTORIES:
        directory=runtime/name;directory.mkdir(mode=0o700);os.chown(directory,65534,65534)
        dirs[name]=directory
    for name in ('media_ro','media_rw','control_dir','private_dir'):
        directory=tree/name;directory.mkdir(mode=0o700);dirs[name]=directory
    cfg=c.Config(version=1,image='anime-db:v6-db40987',uid=65534,gid=65534,
        public_origin='http://anime.test',compose_files=tuple(tree/f'{n}.yaml' for n in (1,2,3)),
        approved_root=tree/'approved',worker_config=tree/'worker.json',snapshot_hook=tree/'snapshot',
        writers_stop_hook=tree/'stop',writers_start_hook=tree/'start',socket_gid=2001,
        nas_source='//nas.test/Anime',**dirs)
    calls=[]
    host=c.Coordinator(cfg,run=lambda argv,**kw:calls.append(argv) or '{"state":"E7_NOT_STARTED"}')
    try:
        host.worker('check-idle','a'*64,'start')
        mounts=[calls[0][i+1] for i,arg in enumerate(calls[0]) if arg=='--mount']
        for name in c.RUNTIME_DIRECTORIES:
            target=cfg.private_dir/'worker-binds'/name
            assert (target.stat().st_dev,target.stat().st_ino)==host._runtime[dirs[name]].identity
            assert any(f'src={target},' in mount for mount in mounts)
            assert not any(f'src={dirs[name]},' in mount for mount in mounts)
        host.status('a'*64,'prepare','STOP','test','test')
        host.progress(stage='stopped',detail='test',active=False)
        host.write_json(cfg.ledger/'test.json',{'ok':True},exclusive=True)
        for path in (cfg.inbox/('a'*64+'.prepare-status.json'),cfg.service_status/'progress.json',cfg.ledger/'test.json'):
            assert path.stat().st_uid==65534
    finally:
        import ctypes
        for name in c.RUNTIME_DIRECTORIES:
            ctypes.CDLL(None).umount2(os.fsencode(cfg.private_dir/'worker-binds'/name),2)
        for pin in host._runtime.values():pin.close()


def test_documented_env_updater_preserves_owner_and_actual_gid(tree):
    import re,textwrap
    repo=Path(__file__).parents[1]
    doc=(repo/'deploy/nexus/docs/RELEASE_UPDATE.md').read_text()
    block=next(textwrap.dedent(b) for b in re.findall(r'```bash\n(.*?)```',doc,re.S) if 'from host_io import open_directory' in b)
    code=block.split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
    code=code.replace("'/usr/local/libexec/anime-db'",repr(str(repo/'deploy/nexus')))
    stack=tree/'stack';stack.mkdir(mode=0o750);os.chown(stack,65534,65534)
    path=stack/'.env';path.write_text('ANIMEDB_UID=65534\nANIMEDB_GID=65534\n')
    path.chmod(0o640);os.chown(path,65534,65534)
    result=subprocess.run([sys.executable,'-I','-',str(stack),'2001'],input=code,text=True,
        env={**os.environ,'SUDO_UID':'65534'},capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert path.read_text()=='ANIMEDB_UID=65534\nANIMEDB_GID=65534\nANIMEDB_CONTROL_GID=2001\n'
    assert (path.stat().st_uid,path.stat().st_gid,path.stat().st_mode & 0o777)==(65534,65534,0o640)
    result=subprocess.run([sys.executable,'-I','-',str(stack),'101'],input=code,text=True,
        env={**os.environ,'SUDO_UID':'65534'},capture_output=True,timeout=10)
    assert result.returncode!=0
    assert 'ANIMEDB_CONTROL_GID=2001' in path.read_text()
