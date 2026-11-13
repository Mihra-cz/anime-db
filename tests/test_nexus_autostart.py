"""Boot uses the coordinator's approved generation and leaves the edge online."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))


def test_boot_and_stop_keep_one_project_and_never_build_or_stop_edge(tmp_path):
    from deploy.nexus import reorganization_autostart as a
    deployment = SimpleNamespace(project_directory=tmp_path/'stack',env_file=tmp_path/'approved/.env',
        project_name='anime-db',files=tuple(tmp_path/'approved'/f'compose.{n}.yaml' for n in range(1,4)))
    config = dict(image='anime-db:v6-db40987',uid=1000,gid=1000,socket_gid=2001)
    calls=[]
    def run(argv, **kwargs):
        calls.append(argv)
        if '--format' in argv:
            return json.dumps(dict(name='anime-db',services=dict(
                app=dict(image=config['image'],user='1000:1000',entrypoint=['python','/usr/local/libexec/anime-db/nexus_first_run.py']),
                edge=dict(image='nginx:stable-alpine@sha256:'+'a'*64,user='101:101',group_add=['2001']))))
        return ''
    a.autostart(deployment,config,'start',run=run)
    assert calls[-2][-6:] == ['up','-d','--no-deps','--no-build','--pull=never','edge']
    assert calls[-1][-6:] == ['up','-d','--no-deps','--no-build','--pull=never','app']
    for call in calls:
        assert call[call.index('-p')+1]=='anime-db'
        assert [call[n+1] for n,x in enumerate(call) if x=='-f']==[str(p) for p in deployment.files]
        assert '--build' not in call and 'pull' not in call
    a.autostart(deployment,config,'stop',run=run)
    assert calls[-1][-4:]==['stop','-t','30','app']
    assert not any('down' in call or 'reorganization_worker' in ' '.join(call) for call in calls)


def test_wrong_boot_image_fails_before_up(tmp_path):
    from deploy.nexus import reorganization_autostart as a
    deployment=SimpleNamespace(project_directory=tmp_path,env_file=tmp_path/'.env',project_name='anime-db',files=(tmp_path/'1',tmp_path/'2',tmp_path/'3'))
    calls=[]
    def run(argv, **kwargs):
        calls.append(argv)
        return json.dumps(dict(name='anime-db',services=dict(app=dict(image='anime-db:nexus'),edge={})))
    with pytest.raises(ValueError):
        a.autostart(deployment,dict(image='anime-db:v6-db40987',uid=1000,gid=1000,socket_gid=2001),'start',run=run)
    assert len(calls)==1 and 'up' not in calls[0]


def test_host_dropin_preserves_audited_firewall_dependency_and_app_only_stop():
    root=Path(__file__).parents[1]/'deploy/nexus'
    dropin=(root/'systemd/anime-db.service.d/approved-production.conf').read_text()
    script=(root/'scripts/autostart.sh').read_text()
    assert 'ExecStart=\nExecStart=/usr/local/libexec/anime-db/autostart.sh' in dropin
    assert 'ExecStop=\nExecStop=/usr/bin/python3' in dropin
    assert '\nRequires=' not in dropin and '\nAfter=' not in dropin
    assert '/usr/local/sbin/animedb-lan-fw apply' in script
    assert 'animedb-lan-only' in script and '*,ro,*' in script
    assert 'reorganization_autostart.py start' in script
    assert 'docker compose' not in script


def test_active_source_commit_read_never_executes_git(tmp_path):
    from deploy.nexus import reorganization_autostart as a
    src=tmp_path/'src'; git=src/'.git'; (git/'refs/heads').mkdir(parents=True)
    (git/'HEAD').write_text('ref: refs/heads/main\n')
    (git/'refs/heads/main').write_text('d'*40+'\n')
    assert a.active_commit(src)=='d'*40
    (git/'refs/heads/main').unlink()
    (git/'packed-refs').write_text('e'*40+' refs/heads/main\n')
    assert a.active_commit(src)=='e'*40
    (git/'HEAD').write_text('ref: refs/heads/../../outside\n')
    with pytest.raises(ValueError): a.active_commit(src)


@pytest.fixture
def boot(tmp_path,monkeypatch):
    from deploy.nexus import reorganization_autostart as a
    src=tmp_path/'projects/anime-db/src'
    edge=src/'deploy/nexus/service-edge/compose.edge.yaml';edge.parent.mkdir(parents=True)
    git=src/'.git';git.mkdir();(git/'HEAD').write_text('db40987'+'a'*33)
    stack=tmp_path/'stacks/anime-db';stack.mkdir(parents=True)
    files=(stack/'compose.yaml',edge,stack/'compose.v6.yaml')
    root=tmp_path/'approved';root.mkdir()
    approved=tuple(root/f'compose.{n}.yaml' for n in (1,2,3))
    for n,(live,copy) in enumerate(zip(files,approved)):
        live.write_text(f'services: {{test: {n}}}\n');copy.write_bytes(live.read_bytes())
    env=stack/'.env';env.write_text('ANIMEDB_UID=1000\n')
    (root/'.env').write_bytes(env.read_bytes())
    (root/'deployment.json').write_text(json.dumps(dict(env_source=str(env))))
    config=tmp_path/'config.json';config.write_text(json.dumps(dict(compose_files=[str(p) for p in files],
        approved_root=str(root),image='anime-db:v6-db40987')))
    deployment=SimpleNamespace(project_directory=stack,files=approved,env_file=root/'.env',project_name='anime-db')
    monkeypatch.setattr(a,'secure_root_path',lambda path: None)  # Root ownership covered separately.
    monkeypatch.setattr(a,'resolve_deployment',lambda *args,**kwargs:deployment)
    return a,config,files,env,src,deployment


def test_boot_selection_matches_active_src_and_exact_three_inputs(boot):
    a,config,files,env,src,deployment=boot
    result,settings=a.load_boot(config)
    assert result is deployment and settings['image']=='anime-db:v6-db40987'


@pytest.mark.parametrize('changed',['base','edge','override','env','release','missing'])
def test_boot_source_mismatch_or_missing_approval_is_stop(boot,changed,monkeypatch):
    a,config,files,env,src,deployment=boot
    if changed in ('base','edge','override'):
        files[('base','edge','override').index(changed)].write_text('different')
    elif changed=='env':env.write_text('ANIMEDB_UID=2000\n')
    elif changed=='release':(src/'.git/HEAD').write_text('f'*40)
    else:
        def missing(*args,**kwargs):raise FileNotFoundError('no approved selection')
        monkeypatch.setattr(a,'resolve_deployment',missing)
    with pytest.raises((ValueError,FileNotFoundError)):a.load_boot(config)


def test_stop_uses_trusted_selection_after_live_checkout_changes(boot):
    a,config,files,env,src,deployment=boot
    (src/'.git/HEAD').write_text('f'*40)
    files[0].write_text('changed')
    result,_=a.load_boot(config,verify_live=False)
    assert result is deployment


def test_release_start_checks_v2_source_hashes_before_binds_and_compose(boot,monkeypatch):
    import hashlib
    a,config,files,env,src,deployment=boot
    record=deployment.files[0].parent/'deployment.json'
    metadata=json.loads(record.read_text())
    metadata.update(version=2,source_sha256=[hashlib.sha256(p.read_bytes()).hexdigest() for p in files])
    record.write_text(json.dumps(metadata))
    # Derived copy is intentionally different. V2 must use approved source SHA.
    deployment.files[0].write_text('derived copy with private source path')
    settings=json.loads(config.read_text());settings.update(uid=1000,gid=1000,socket_gid=2001)
    config.write_text(json.dumps(settings))
    events=[]
    verify=a.verify_active_release
    def checked(*args):
        events.append('verify')
        return verify(*args)
    monkeypatch.setattr(a,'verify_active_release',checked)
    monkeypatch.setattr(a,'ensure_bindings',lambda rows:events.append('bind'))
    def run(argv,**kwargs):
        if 'config' in argv:
            events.append('model')
            return json.dumps(dict(name='anime-db',services=dict(
                app=dict(image=settings['image'],user='1000:1000',entrypoint=['python','/usr/local/libexec/anime-db/nexus_first_run.py']),
                edge=dict(image='nginx:stable-alpine@sha256:'+'a'*64,user='101:101',group_add=['2001']))))
        events.append(argv[-1])
        return ''
    selected,loaded=a.load_boot(config)
    a.autostart(selected,loaded,'start',run=run)
    assert events==['verify','bind','model','edge','app']
    files[1].write_text('changed live edge')
    events.clear()
    with pytest.raises(ValueError,match='active Compose sources'):
        selected,loaded=a.load_boot(config)
        a.autostart(selected,loaded,'start',run=run)
    assert events==['verify']


def test_release_and_operations_never_start_raw_approved_generation():
    root=Path(__file__).parents[1]/'deploy/nexus/docs'
    release=(root/'RELEASE_UPDATE.md').read_text()
    for fragment in (release.split('### C.')[1].split('### D.')[0], release.split('### E.')[1].split('## 3.')[0]):
        assert 'reorganization_autostart.py start' in fragment
        assert '$REVIEWED_GENERATION/compose.' not in fragment
    operations=(root/'OPERATIONS.md').read_text()
    assert '--env-file "$STACK_DIR/.env" -p anime-db' in operations
    assert 'reorganization_autostart.py start' in operations


def test_boot_waits_for_slow_normal_admission_without_restarting_app(tmp_path):
    import subprocess
    root=Path(__file__).parents[1]/'deploy/nexus'
    script=(root/'scripts/autostart.sh').read_text()
    poll=script[script.index('deadline='):]
    count=tmp_path/'count';count.write_text('0')
    prefix=r'''
    curl() {
      n=$(<"$COUNT")
      echo $((n + 1)) > "$COUNT"
      if (( n >= 20 )); then echo '{"status":"ok"}'; else return 22; fi
    }
    sleep() { SECONDS=$((SECONDS + 30)); }
    '''
    result=subprocess.run(['bash','-c',prefix+'\n'+poll],env={**__import__('os').environ,'COUNT':str(count)},
        capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr
    assert 'START PASS' in result.stdout and count.read_text().strip()=='21'
    dropin=(root/'systemd/anime-db.service.d/approved-production.conf').read_text()
    budget=int(dropin.split('TimeoutStartSec=')[1].splitlines()[0])
    assert budget > 900 + 3*120 + 20
    assert 'reorganization_autostart' not in poll and ' up ' not in poll
