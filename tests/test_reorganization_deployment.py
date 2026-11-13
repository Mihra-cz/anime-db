"""Approved copies keep privileged Compose independent of user-owned src/."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

MODULE = Path(__file__).parents[1] / 'deploy/nexus/reorganization_deployment.py'


def api():
    spec = importlib.util.spec_from_file_location('reorganization_deployment', MODULE)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    m = api()
    # Only root ownership is simulated; actual paths, mode checks, nofollow,
    # exact copies, fingerprints and atomic updates are exercised on disk.
    monkeypatch.setattr(m, '_require_root', lambda: None)
    original = m._trusted_path
    def trusted(path, *, directory=False):
        path = Path(path)
        assert path == tmp_path/'stack' or path.is_relative_to(tmp_path/'stack'/'approved-v6')
        for component in (path, *path.parents):
            if component == tmp_path / 'stack': break
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
                raise ValueError('unsafe trusted path')
        if directory and not path.is_dir(): raise ValueError('directory required')
        if not directory and not path.is_file(): raise ValueError('file required')
    monkeypatch.setattr(m, '_trusted_path', trusted)
    source = tmp_path / 'src'; source.mkdir(mode=0o775); source.chmod(0o775)
    releases = tmp_path / 'releases'; releases.mkdir(mode=0o755)
    stack = tmp_path / 'stack'; stack.mkdir(mode=0o755)
    files = (stack/'compose.yaml', source/'compose.edge.yaml', stack/'compose.v6.yaml')
    for i, path in enumerate(files): path.write_text('services:\n  app:\n    image: anime-db:v6-0123456\n')
    env = stack/'.env'; env.write_text('ANIMEDB_UID=1000\nANIMEDB_GID=1000\n')
    return m, files, env


def test_stage_and_approval_preserve_user_source_ownership_and_exact_order(deployment):
    m, files, env = deployment
    owner = (files[1].parent.stat().st_uid, files[1].parent.stat().st_mode)
    generation = m.stage_deployment(files, env_file=env)
    assert not (files[0].parent/'approved-v6/current.json').exists()
    digest = m.generation_sha256(generation)
    m.approve_deployment(generation, reviewed_sha256=digest)
    current = m.resolve_deployment(files)
    assert current.project_name == 'anime-db' and current.project_directory == files[0].parent
    assert [p.read_bytes() for p in current.files] == [p.read_bytes() for p in files]
    assert current.env_file.read_bytes() == env.read_bytes()
    assert (files[1].parent.stat().st_uid, files[1].parent.stat().st_mode) == owner
    assert stat.S_IMODE(files[1].parent.stat().st_mode) == 0o775
    assert all(p.parent == generation for p in current.files)


def test_live_user_compose_change_cannot_change_privileged_input(deployment):
    m, files, env = deployment
    generation = m.stage_deployment(files, env_file=env)
    m.approve_deployment(generation, reviewed_sha256=m.generation_sha256(generation))
    approved = m.resolve_deployment(files).files[1].read_bytes()
    files[1].write_text('services:\n  attacker:\n    privileged: true\n')
    assert m.resolve_deployment(files).files[1].read_bytes() == approved


@pytest.mark.parametrize('target', ['compose', 'manifest', 'env'])
def test_modified_approved_bytes_are_rejected(deployment, target):
    m, files, env = deployment
    generation = m.stage_deployment(files, env_file=env)
    m.approve_deployment(generation, reviewed_sha256=m.generation_sha256(generation))
    path = generation / {'compose':'compose.2.yaml','manifest':'deployment.json','env':'.env'}[target]
    path.chmod(0o600); path.write_text('{}\n')
    with pytest.raises(ValueError): m.resolve_deployment(files)


def test_wrong_review_sha_never_activates_snapshot(deployment):
    m, files, env = deployment
    generation = m.stage_deployment(files, env_file=env)
    with pytest.raises(ValueError): m.approve_deployment(generation, reviewed_sha256='0'*64)
    assert not (generation.parent/'current.json').exists()


@pytest.mark.parametrize('text', ['include: /mutable.yaml\n', 'services:\n  app:\n    env_file: /mutable.env\n', '{"include":"/mutable.yaml"}\n', 'services:\n  app:\n    extends: {file: /mutable.yaml}\n'])
def test_external_compose_inputs_are_not_silently_left_live(deployment, text):
    m, files, env = deployment
    files[1].write_text(text)
    with pytest.raises(ValueError): m.stage_deployment(files, env_file=env)


def test_boot_tmpfiles_directory_exists_without_coordinator_and_can_be_recreated(tmp_path):
    shipped = Path(__file__).parents[1]/'deploy/nexus/tmpfiles.d/animedb-reorganization.conf'
    rule = shipped.read_text()
    assert 'd /run/animedb-reorganization 0750 root animedb-control - -' in rule
    # Exercise real tmpfiles under an alternate root, substituting only the
    # unavailable root/group ownership with this test user's UID/GID.
    root = tmp_path/'boot'; conf = root/'etc/tmpfiles.d'; conf.mkdir(parents=True)
    (root/'run').mkdir()
    local = conf/'animedb-reorganization.conf'
    local.write_text(f'd /run/animedb-reorganization 0750 {os.getuid()} {os.getgid()} - -\n')
    command = ['systemd-tmpfiles','--create','--root='+str(root),str(local)]
    result = subprocess.run(command,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    directory = root/'run/animedb-reorganization'
    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    directory.rmdir()  # Reboot discards /run; no coordinator service is started.
    result = subprocess.run(command,capture_output=True,text=True)
    assert result.returncode == 0 and directory.is_dir(), result.stderr
    assert directory.stat().st_uid == os.getuid() and directory.stat().st_gid == os.getgid()


def test_docker_boot_order_and_service_permissions_do_not_depend_on_coordinator():
    deploy = Path(__file__).parents[1]/'deploy/nexus'
    dependency = (deploy/'systemd/docker.service.d/animedb-runtime.conf').read_text()
    assert 'After=systemd-tmpfiles-setup.service' in dependency
    assert 'Requires=systemd-tmpfiles-setup.service' in dependency
    unit = (deploy/'systemd/animedb-reorganization.service').read_text()
    for option in ('ProtectSystem=', 'ReadWritePaths=', 'PrivateTmp=', 'ProtectHome=',
                   'ProtectKernelTunables=', 'ProtectKernelModules=', 'ProtectControlGroups='):
        assert option not in unit
    assert 'RuntimeDirectory=' not in unit
    assert 'Group=animedb-control' in unit


def test_yaml_key_alias_cannot_reintroduce_a_live_compose_include(deployment):
    m, files, env = deployment
    files[1].write_text('x-name: &external include\n*external: /mutable.yaml\n')
    with pytest.raises(ValueError): m.stage_deployment(files,env_file=env)


def test_current_base_edge_and_uid_env_fit_the_snapshot_contract():
    m = api()
    root = Path(__file__).parents[1]/'deploy/nexus'
    m._check_inputs([(root/'compose.yaml').read_bytes(),(root/'service-edge/compose.edge.yaml').read_bytes(),
        b'services:\n  app:\n    image: anime-db:v6-0123456\n    build: !reset null\n'],
        (root/'.env.example').read_bytes())


def test_approved_copies_keep_same_compose_model_and_relative_path_context(deployment):
    import shutil
    if not shutil.which('docker'): pytest.skip('Docker Compose client unavailable')
    m,files,env = deployment
    files[0].write_text('name: anime-db\nservices:\n  app:\n    image: anime-db:v6-0123456\n    user: "${ANIMEDB_UID}:${ANIMEDB_GID}"\n    volumes: ["./db:/data"]\n')
    files[1].write_text('services:\n  edge:\n    image: nginx:stable-alpine\n')
    files[2].write_text('services:\n  app:\n    image: anime-db:v6-abcdef0\n')
    generation=m.stage_deployment(files,env_file=env)
    m.approve_deployment(generation,reviewed_sha256=m.generation_sha256(generation))
    resolved=m.resolve_deployment(files)
    def rendered(paths,env_path):
        args=['docker','compose','--project-directory',str(files[0].parent),'--env-file',str(env_path),'-p','anime-db']
        for path in paths: args += ['-f',str(path)]
        response=subprocess.run(args+['config','--format','json'],capture_output=True,text=True,timeout=20)
        assert response.returncode==0,response.stderr
        return json.loads(response.stdout)
    original=rendered(files,env)
    assert rendered(resolved.files,resolved.env_file)==original
    assert original['name']=='anime-db' and original['services']['app']['image']=='anime-db:v6-abcdef0'
    assert original['services']['app']['volumes'][0]['source']==str(files[0].parent/'db')


def test_separate_approved_root_keeps_mutable_project_directory(deployment, monkeypatch):
    m, files, env = deployment
    root = files[0].parent.parent/'trusted-generations'
    root.mkdir(mode=0o700)
    old = m._trusted_path
    def trust(path, *, directory=False):
        if Path(path) == root or Path(path).is_relative_to(root):
            info = Path(path).lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
                raise ValueError('unsafe generation')
        else:
            old(path, directory=directory)
    monkeypatch.setattr(m, '_trusted_path', trust)
    owner = files[0].parent.stat()
    generation = m.stage_deployment(files, env_file=env, approved_root=root)
    m.approve_deployment(generation, reviewed_sha256=m.generation_sha256(generation))
    resolved = m.resolve_deployment(files, approved_root=root)
    assert resolved.project_directory == files[0].parent
    assert [p.read_bytes() for p in resolved.files] == [p.read_bytes() for p in files]
    assert (files[0].parent.stat().st_uid, files[0].parent.stat().st_mode) == (owner.st_uid, owner.st_mode)


@pytest.mark.parametrize('source', ['./db:/data', '../db:/data', 'relative:/data'])
def test_separate_root_rejects_relative_bind_sources(deployment, source):
    m, files, env = deployment
    files[0].write_text('services:\n  app:\n    volumes:\n      - '+source+'\n')
    with pytest.raises(ValueError, match='absolute'):
        m._check_inputs([p.read_bytes() for p in files], env.read_bytes(), absolute_binds=True)


@pytest.mark.parametrize('text',[
    'services:\n  app:\n    volumes:\n      - ./db:/data # comment\n',
    'services:\n  app:\n    volumes:\n    - ./db:/data\n',
    'services:\n  app:\n    volumes:\n      - /absolute:/data\n',
])
def test_private_bind_generation_rejects_implicit_or_relative_creation(deployment,text):
    m,_,env=deployment
    with pytest.raises(ValueError):m._check_inputs([text.encode()],env.read_bytes(),absolute_binds=True)
