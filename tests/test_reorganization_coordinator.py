"""Host actions stay fixed; browser approval never becomes a command interface."""
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location('reorganization_coordinator',
    Path(__file__).parents[1] / 'deploy/nexus/reorganization_coordinator.py')


def module():
    value = importlib.util.module_from_spec(SPEC)
    import sys
    sys.modules[SPEC.name] = value
    SPEC.loader.exec_module(value)
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def save(path, value):
    path.write_bytes(encode(value))
    path.chmod(0o600)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    m = module()
    directories = {name: tmp_path / name for name in (
        'database_dir', 'state_dir', 'media_ro', 'media_rw', 'inbox', 'outbox',
        'ledger', 'backups', 'service_status', 'control_dir', 'private_dir')}
    for path in directories.values():
        path.mkdir(mode=0o700)
    config = m.Config(version=1, image='anime-db:v6-0123456789ab', uid=os.getuid(), gid=os.getgid(),
        public_origin='http://anime.test:8000', compose_files=tuple(tmp_path / f'compose-{n}.yaml' for n in range(3)),
        worker_config=tmp_path / 'worker.json', snapshot_hook=tmp_path / 'snapshot',
        writers_stop_hook=tmp_path / 'stop-writers', writers_start_hook=tmp_path / 'start-writers',
        socket_gid=os.getgid(), nas_source='//nas.test/Anime',runtime_evidence=tmp_path/'runtime-evidence.json', **directories)
    from app.target_execution_manifest import RuntimeEvidence
    from dataclasses import asdict
    save(config.runtime_evidence,asdict(RuntimeEvidence(mount_identity='fixture-mount',mount_read_write_checked=True,
        safe_write_capability_proven=True,rename_noreplace_capability_proven=True)))
    monkeypatch.setattr(m,'host_mount_identity',lambda *args:'fixture-mount')
    monkeypatch.setattr(m,'resolve_deployment',lambda files:SimpleNamespace(files=files,env_file=files[0].parent/'.env',
        project_directory=files[0].parent,project_name='anime-db'))
    calls = []
    source, approved, manifest = 'b' * 64, 'c' * 64, 'd' * 64
    def run(argv, *, input=None, timeout=120):
        calls.append((argv, input))
        if 'config' in argv:
            return config.image + '\n'
        if argv[0] == str(config.writers_stop_hook):
            return '{"writers_stopped":true}'
        if argv[0] == str(config.writers_start_hook):
            return '{"writers_started":true}'
        if argv[0] == str(config.snapshot_hook):
            binding = json.loads(input)
            return json.dumps(dict(snapshot_id='snapshot-1', snapshot_at=binding['created_at'],
                snapshot_manifest_id=binding['manifest_id']))
        if 'app.tools.reorganization_worker' in argv:
            verb = argv[-3]
            if verb in {'check-start','check-idle'}:
                return json.dumps(dict(state='READY' if verb=='check-start' else 'E7_NOT_STARTED',source_sha256=source,approved_sha256=approved,manifest_id=manifest))
            if verb == 'plan':
                return json.dumps(dict(manifest_id=manifest, created_at='2026-11-10T12:00:00Z', source_sha256=source))
            if verb == 'publish':
                return json.dumps(dict(source_sha256=source))
            if verb == 'execute':
                result = dict(state='NORMAL',source_sha256=source,approved_sha256=approved,manifest_id=manifest)
                save(config.inbox / (source + '.result.json'), result)
                return json.dumps(result)
        return ''
    monkeypatch.setattr(m, 'require_rw_mount', lambda *args: None)
    coordinator = m.Coordinator(config, run=run, healthy=lambda url: True)
    return m, config, coordinator, calls, source, approved, manifest


def request(config, kind='prepare', source='a'*64, approved='c'*64, manifest='d'*64):
    data = dict(version=1, kind=kind, request_id=source, actor='Mirek', requested_at=datetime.now(timezone.utc).isoformat())
    data.update(preview_db_sha256='e'*64) if kind == 'prepare' else data.update(
        source_sha256=source, approved_sha256=approved, manifest_id=manifest)
    save(config.outbox / f'{source}.{kind}-request.json', data)
    return data


def status(config, request_id, kind):
    return json.loads((config.inbox / f'{request_id}.{kind}-status.json').read_bytes())


def test_prepare_claim_precedes_all_actions_and_restarts_only_after_publication(setup):
    m, c, host, calls, source, _, _ = setup
    request(c)
    original = host.run
    def checked(argv, **kwargs):
        assert (c.private_dir / ('a'*64 + '.prepare-claim.json')).exists()
        return original(argv, **kwargs)
    host.run = checked
    host.tick()
    assert status(c, 'a'*64, 'prepare')['state'] == 'READY'
    assert status(c, 'a'*64, 'prepare')['source_sha256'] == source
    verbs = [argv[-3] for argv, _ in calls if 'app.tools.reorganization_worker' in argv]
    assert verbs == ['plan', 'publish']
    snapshot = next(json.loads(stdin) for argv, stdin in calls if argv[0] == str(c.snapshot_hook))
    assert snapshot['source_sha256'] == source
    assert all(isinstance(argv, list) for argv, _ in calls)
    assert calls[-2][0][-6:] == ['up', '-d', '--no-deps', '--no-build', '--pull=never', 'app']
    before = list(calls)
    host.tick()
    assert calls == before
    assert json.loads((c.service_status / 'progress.json').read_bytes())['app_ready']


def test_explicit_start_alone_executes_and_verified_normal_restarts_app(setup):
    _, c, host, calls, source, approved, manifest = setup
    request(c, 'start', source, approved, manifest)
    host.tick()
    assert [a[-3] for a, _ in calls if 'app.tools.reorganization_worker' in a] == ['check-start','execute']
    assert status(c, source, 'start')['state'] == 'NORMAL'
    progress = json.loads((c.service_status / 'progress.json').read_bytes())
    assert progress['app_ready'] is True and progress['result_url'] == '/reorganization/approval/' + source
    worker = next(a for a, _ in calls if 'app.tools.reorganization_worker' in a and a[-3]=='execute')
    assert '--network=none' in worker and '--read-only' in worker
    assert '--entrypoint=python' in worker and c.image in worker
    assert not any('docker.sock' in part for part in worker)
    assert f'type=bind,src={c.media_rw},dst=/media/anime' in worker
    assert f'type=bind,src={c.outbox},dst=/approval-outbox,readonly' in worker


def test_worker_stop_keeps_app_stopped_and_spent_claim_is_not_retried(setup):
    _, c, host, calls, source, approved, manifest = setup
    request(c, 'start', source, approved, manifest)
    original = host.run
    def failed(argv, **kwargs):
        if 'app.tools.reorganization_worker' in argv and argv[-3] in {'execute','check-idle'}:
            raise RuntimeError('sensitive credentials /secret/path')
        return original(argv, **kwargs)
    host.run = failed
    host.tick()
    data = status(c, source, 'start')
    assert data['state'] == 'STOP' and 'secret' not in json.dumps(data)
    assert not any('up' in a for a, _ in calls)
    count = len(calls)
    host.tick()
    assert len(calls) == count


def test_restart_recovers_claim_as_stop_without_repeating_worker(setup):
    m, c, _, calls, source, approved, manifest = setup
    request(c, 'start', source, approved, manifest)
    save(c.private_dir / (source + '.start-claim.json'), dict(version=1, kind='start', request_id=source))
    host = m.Coordinator(c, run=lambda argv, **kwargs: calls.append((argv, None)) or '', healthy=lambda url: True)
    host.recover()
    host.tick()
    assert status(c, source, 'start')['state'] == 'STOP'
    assert not any('app.tools.reorganization_worker' in a for a, _ in calls)
    assert not any('up' in a for a, _ in calls)
    assert any(a[1:3] == ['stop', '--time=10'] for a, _ in calls)


def test_missing_rw_mount_fails_before_any_worker_or_app_stop(setup, monkeypatch):
    m, c, host, calls, *_ = setup
    request(c)
    def unavailable(*args):
        raise ValueError('missing mount')
    monkeypatch.setattr(m, 'require_rw_mount', unavailable)
    host.tick()
    assert status(c, 'a'*64, 'prepare')['state'] == 'STOP'
    assert not calls


def test_normal_without_matching_original_result_cannot_restart(setup):
    _, c, host, calls, source, approved, manifest = setup
    request(c, 'start', source, approved, manifest)
    original = host.run
    def unbound(argv, **kwargs):
        result = original(argv, **kwargs)
        if 'app.tools.reorganization_worker' in argv and argv[-3]=='execute':
            (c.inbox / (source + '.result.json')).unlink()
        return result
    host.run = unbound
    host.tick()
    assert status(c, source, 'start')['state'] == 'STOP'
    assert not any('up' in a for a, _ in calls)


def test_normal_with_failed_health_preserves_normal_but_no_app_ready(setup):
    _, c, host, _, source, approved, manifest = setup
    request(c, 'start', source, approved, manifest)
    host.healthy = lambda url: False
    host.health_timeout = 0
    host.tick()
    assert status(c, source, 'start')['state'] == 'NORMAL'
    assert status(c, source, 'start')['phase'] == 'app_restart_failed'
    data = json.loads((c.service_status / 'progress.json').read_bytes())
    assert data['outcome'] == 'NORMAL' and not data['app_ready']


def test_wrong_image_and_injected_request_fields_never_launch_worker(setup):
    _, c, host, calls, *_ = setup
    data = request(c)
    data['command'] = 'rm -rf /'
    save(c.outbox / ('a'*64 + '.prepare-request.json'), data)
    host.tick()
    assert not calls and status(c, 'a'*64, 'prepare')['state'] == 'STOP'
    request(c, source='f'*64)
    host.run = lambda *args, **kwargs: 'anime-db:latest'
    host.tick()
    assert status(c, 'f'*64, 'prepare')['state'] == 'STOP'


def proposal_record(source, approved):
    proposal = dict(candidates=[dict(path='Old', identity=[1,2], disposition='empty_after_actions')],
        before_snapshot=[dict(path='Old',kind='directory')], root='/media/anime')
    proposal['proposal_sha256'] = hashlib.sha256(encode(proposal)).hexdigest()
    return dict(source_sha256=source, approved_sha256=approved, proposal=proposal)


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=3)
        self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def http(server, method='GET', payload=None, headers=None, path='/cleanup'):
    connection = UnixHTTP(server.server_address)
    headers = headers or {}
    if payload is not None:
        headers.setdefault('Content-Type','application/json')
    connection.request(method, path, body=encode(payload) if payload is not None else None, headers=headers)
    response = connection.getresponse()
    result = response.status, json.loads(response.read()), dict(response.getheaders())
    connection.close()
    return result


def test_unix_cleanup_exact_bound_cookie_csrf_one_use_and_no_command_surface(setup):
    m, c, host, _, source, approved, _ = setup
    host.active_source = source
    record = proposal_record(source, approved)
    save(c.ledger / (source + '.cleanup-request.json'), record)
    server = m.make_control_server(host)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        code, view, headers = http(server)
        assert code == 200 and view['waiting'] and view['proposal_sha256']==record['proposal']['proposal_sha256']
        cookie = headers['Set-Cookie'].split(';')[0]
        _,view,_=http(server,headers={'Cookie':cookie},path='/cleanup/proposal?sha='+record['proposal']['proposal_sha256'])
        body = dict(source_sha256=source, approved_sha256=approved,
            proposal_sha256=record['proposal']['proposal_sha256'], actor='Mirek',directories=['Old'],confirm=True,csrf=view['csrf'])
        assert http(server,'POST',body,{'Origin':'http://evil.test','Cookie':cookie})[0] == 403
        assert http(server,'POST',body,{'Origin':c.public_origin})[0] == 403
        assert http(server,'POST',{**body,'approved_sha256':'0'*64},{'Origin':c.public_origin,'Cookie':cookie})[0] == 409
        assert http(server,'POST',{**body,'directories':['../../secret']},{'Origin':c.public_origin,'Cookie':cookie})[0] == 400
        assert http(server,'POST',body,{'Origin':c.public_origin,'Cookie':cookie})[0] == 200
        decision = json.loads((c.ledger / (source + '.cleanup-decision.json')).read_bytes())
        assert decision == {key:value for key,value in body.items() if key != 'csrf'}
        assert http(server,'POST',body,{'Origin':c.public_origin,'Cookie':cookie})[0] == 409
        consumed=http(server)[1]
        assert consumed['waiting'] is False and consumed['decision_state']=='CONSUMED'
    finally:
        server.shutdown(); server.server_close(); thread.join()


@pytest.mark.parametrize('directories,decision', [([],None),([], 'stop')])
def test_cleanup_preserve_all_and_stop_are_explicit_decisions(setup,directories,decision):
    _, c, host, _, source, approved, _ = setup
    host.active_source=source
    record=proposal_record(source,approved)
    save(c.ledger/(source+'.cleanup-request.json'),record)
    view,cookie=host.cleanup_proposal(None,record['proposal']['proposal_sha256'])
    body=dict(source_sha256=source,approved_sha256=approved,proposal_sha256=record['proposal']['proposal_sha256'],
        actor='Mirek',directories=directories,confirm=True,csrf=view['csrf'])
    if decision: body['decision']=decision
    host.cleanup_decide(body,origin=c.public_origin,cookie=cookie)
    assert json.loads((c.ledger/(source+'.cleanup-decision.json')).read_bytes())['directories']==[]


def test_stale_cleanup_proposal_and_parent_without_child_are_rejected(setup):
    _,c,host,_,source,approved,_=setup
    host.active_source=source
    record=proposal_record(source,approved)
    save(c.ledger/(source+'.cleanup-request.json'),record)
    view,cookie=host.cleanup_proposal(None,record['proposal']['proposal_sha256'])
    body=dict(source_sha256=source,approved_sha256=approved,proposal_sha256=record['proposal']['proposal_sha256'],
        actor='Mirek',directories=['Old'],confirm=True,csrf=view['csrf'])
    record['proposal']['before_snapshot'].append(dict(path='Old/child',kind='directory'))
    proposal=record['proposal']
    proposal['proposal_sha256']=hashlib.sha256(encode({k:v for k,v in proposal.items() if k!='proposal_sha256'})).hexdigest()
    save(c.ledger/(source+'.cleanup-request.json'),record)
    with pytest.raises(Exception):host.cleanup_decide(body,origin=c.public_origin,cookie=cookie)
    view,cookie=host.cleanup_proposal(cookie,record['proposal']['proposal_sha256'])
    body.update(csrf=view['csrf'],proposal_sha256=proposal['proposal_sha256'])
    with pytest.raises(ValueError):host.cleanup_decide(body,origin=c.public_origin,cookie=cookie)
    assert not (c.ledger/(source+'.cleanup-decision.json')).exists()


def test_config_rejects_non_root_writable_symlink_and_unknown_fields(tmp_path):
    m=module()
    path=tmp_path/'config.json'
    save(path,{'version':1,'command':'unsafe'})
    with pytest.raises(ValueError):m.load_config(path)
    link=tmp_path/'link.json';link.symlink_to(path)
    with pytest.raises(ValueError):m.load_config(link)


def test_unconfirmed_writer_fence_never_runs_worker(setup):
    _,c,host,calls,*_=setup
    request(c)
    original=host.run
    def no_fence(argv,**kwargs):
        if argv[0]==str(c.writers_stop_hook):return '{}'
        return original(argv,**kwargs)
    host.run=no_fence
    host.tick()
    assert status(c,'a'*64,'prepare')['state']=='STOP'
    assert status(c,'a'*64,'prepare')['phase']=='writers'
    assert not any('app.tools.reorganization_worker' in argv for argv,_ in calls)


def test_snapshot_failure_is_safe_specific_stop_and_restores_guarded_app(setup):
    _,c,host,calls,*_=setup
    request(c)
    original=host.run
    def bad_snapshot(argv,**kwargs):
        if argv[0]==str(c.snapshot_hook):return '{}'
        return original(argv,**kwargs)
    host.run=bad_snapshot
    host.tick()
    data=status(c,'a'*64,'prepare')
    assert data['state']=='STOP' and data['phase']=='snapshot'
    assert 'snapshot' in data['detail'].lower()
    assert any('up' in argv for argv,_ in calls)
    progress=json.loads((c.service_status/'progress.json').read_bytes())
    assert progress['app_ready'] and progress['outcome']=='STOP'
    assert progress['result_url']=='/reorganization/jobs/'+'a'*64


def test_invalid_mount_stop_explains_required_rw_integration(setup,monkeypatch):
    m,c,host,_,*_=setup
    request(c)
    monkeypatch.setattr(m,'require_rw_mount',lambda *a: (_ for _ in ()).throw(ValueError('private NAS path')))
    host.tick()
    detail=status(c,'a'*64,'prepare')['detail']
    assert 'RW' in detail and 'private' not in detail


def test_worker_failure_stops_fixed_container_and_preserves_private_logs(setup):
    _,c,host,calls,source,approved,manifest=setup
    request(c,'start',source,approved,manifest)
    original=host.run
    def failed(argv,**kwargs):
        if 'app.tools.reorganization_worker' in argv:
            if argv[-3]=='execute':raise RuntimeError('private worker failure')
            if argv[-3]=='check-start':return original(argv,**kwargs)
            raise RuntimeError('private worker failure')
        return original(argv,**kwargs)
    host.run=failed
    host.tick()
    assert any(argv[1:3]==['stop','--time=10'] and argv[-1].endswith('-execute') for argv,_ in calls)
    assert 'private worker failure' in (c.private_dir/(source+'.start-error.json')).read_text()


def test_create_once_records_publish_complete_bytes_atomically(setup,monkeypatch):
    m,c,*_=setup
    target=c.ledger/'atomic.json'
    seen=[]
    original=os.fsync
    def observed(fd):
        if not os.path.isdir('/proc/self/fd/'+str(fd)):
            seen.append(target.exists())
        return original(fd)
    monkeypatch.setattr(os,'fsync',observed)
    m.write_json(target,{'complete':True},exclusive=True)
    assert seen==[False]
    with pytest.raises(FileExistsError):m.write_json(target,{'complete':False},exclusive=True)
    assert json.loads(target.read_bytes())=={'complete':True}


def test_worker_config_must_match_fixed_container_bindings(setup):
    m,c,*_=setup
    data=dict(database='/data/anime.db',library_root='/media/anime',share=c.nas_source,share_root='/',
        windows_root='Z:\\',inbox='/approval-inbox',outbox='/approval-outbox',ledger='/approval-ledger',
        runtime_lock='/state/runtime.lock',backups='/backups',service='/service-status',
        mount_read_write_checked=False,safe_write_capability_proven=False,rename_noreplace_capability_proven=False)
    save(c.worker_config,data)
    m.verify_worker_config(c)
    data['database']='/outside/unmounted.db'
    save(c.worker_config,data)
    with pytest.raises(ValueError):m.verify_worker_config(c)


def test_prepare_heartbeat_changes_timestamp_then_stops_without_overwriting_worker(setup,monkeypatch):
    m,c,host,*_=setup
    monkeypatch.setattr(m,'HEARTBEAT_SECONDS',0.01)
    host.progress(stage='preparing',detail='Host preparation')
    host.begin_heartbeat()
    try:
        import time
        path=c.service_status/'progress.json'
        first=json.loads(path.read_bytes())
        deadline=time.monotonic()+2
        while json.loads(path.read_bytes())['updated_at']==first['updated_at']:
            assert time.monotonic()<deadline
            time.sleep(0.01)
        assert json.loads(path.read_bytes())['run_id']==first['run_id']
    finally:host.end_heartbeat()
    saved=json.loads(path.read_bytes())
    import time
    time.sleep(0.04)
    assert json.loads(path.read_bytes())==saved


def test_control_accepts_bounded_large_explicit_candidate_decision(setup):
    m,c,host,_,source,approved,_=setup
    host.active_source=source
    record=proposal_record(source,approved)
    paths=['Old/'+str(i)+'界'*90 for i in range(279)]
    record['proposal']['candidates']=[dict(path=p,identity=[1,i],disposition='empty_after_actions') for i,p in enumerate(paths)]
    record['proposal']['before_snapshot']=[dict(path=p,kind='directory') for p in paths]
    body=record['proposal']
    body['proposal_sha256']=hashlib.sha256(encode({k:v for k,v in body.items() if k!='proposal_sha256'})).hexdigest()
    save(c.ledger/(source+'.cleanup-request.json'),record)
    server=m.make_control_server(host)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        _,state,headers=http(server)
        cookie=headers['Set-Cookie'].split(';')[0]
        _,view,_=http(server,headers={'Cookie':cookie},path='/cleanup/proposal?sha='+body['proposal_sha256'])
        decision=dict(source_sha256=source,approved_sha256=approved,proposal_sha256=body['proposal_sha256'],
            actor='Mirek',directories=paths,confirm=True,csrf=view['csrf'])
        assert len(encode(decision))>64*1024
        assert http(server,'POST',decision,{'Origin':c.public_origin,'Cookie':cookie})[0]==200
    finally:server.shutdown();server.server_close();thread.join()


def test_cleanup_actor_limit_matches_original_worker_without_consuming_decision(setup):
    _,c,host,_,source,approved,_=setup
    host.active_source=source
    record=proposal_record(source,approved)
    save(c.ledger/(source+'.cleanup-request.json'),record)
    view,cookie=host.cleanup_proposal(None,record['proposal']['proposal_sha256'])
    data=dict(source_sha256=source,approved_sha256=approved,proposal_sha256=record['proposal']['proposal_sha256'],
        actor='X'*129,directories=[],confirm=True,csrf=view['csrf'])
    with pytest.raises(ValueError):host.cleanup_decide(data,origin=c.public_origin,cookie=cookie)
    assert not (c.private_dir/(source+'.cleanup-claim.json')).exists()


def test_compose_uses_explicit_root_controlled_project_envfile(setup):
    _,c,host,*_=setup
    argv=host.compose('config','--images','app')
    assert argv[2:6]==['--project-directory',str(c.compose_files[0].parent),'--env-file',str(c.compose_files[0].parent/'.env')]
    assert '-p' in argv and argv[argv.index('-p')+1]=='anime-db'
    assert [argv[i+1] for i,p in enumerate(argv) if p=='-f']==[str(p) for p in c.compose_files]


def test_cleanup_detail_omits_large_integer_snapshot_without_changing_seal(setup):
    m,c,host,_,source,approved,_=setup
    host.active_source=source
    record=proposal_record(source,approved)
    record['proposal']['before_snapshot'][0]['mtime_ns']=1794312345678901234
    body={key:value for key,value in record['proposal'].items() if key!='proposal_sha256'}
    record['proposal']['proposal_sha256']=hashlib.sha256(encode(body)).hexdigest()
    save(c.ledger/(source+'.cleanup-request.json'),record)
    view,_=host.cleanup_proposal(None,record['proposal']['proposal_sha256'])
    assert view['proposal']['proposal_sha256']==record['proposal']['proposal_sha256']
    assert 'before_snapshot' not in view['proposal'] and 'proposal_canonical' not in view


def test_normal_result_with_279_long_unicode_cleanup_paths_restarts_app(setup):
    m, c, host, calls, source, approved, manifest = setup
    request(c,'start',source,approved,manifest)
    original = host.run
    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if 'app.tools.reorganization_worker' in argv and argv[-3]=='execute':
            record = m.read_json(c.inbox / (source + '.result.json'))
            record['removed_directories'] = ['日本語の非常に長い元のディレクトリ/'*8+str(i) for i in range(279)]
            save(c.inbox / (source + '.result.json'), record)
            assert (c.inbox / (source + '.result.json')).stat().st_size > m.MAX_REQUEST
        return result
    host.run = run
    host.tick()
    assert status(c,source,'start')['state'] == 'NORMAL'
    assert json.loads((c.service_status/'progress.json').read_bytes())['app_ready'] is True


def test_public_progress_remains_readable_under_systemd_private_umask(setup):
    _, c, host, *_ = setup
    previous = os.umask(0o077)
    try:
        host.progress(stage='preparing',detail='Příprava manifestu')
    finally:
        os.umask(previous)
    import stat
    assert stat.S_IMODE((c.service_status/'progress.json').stat().st_mode) == 0o644


def test_stale_start_queue_is_rejected_before_any_app_stop(setup):
    from datetime import datetime, timezone, timedelta
    _, c, host, calls, source, approved, manifest = setup
    data = request(c, 'start', source, approved, manifest)
    data['requested_at'] = (datetime.now(timezone.utc)-timedelta(seconds=901)).isoformat()
    save(c.outbox/(source+'.start-request.json'), data)
    host.tick()
    assert not calls
    assert status(c,source,'start')['error_code'] == 'REQUEST_EXPIRED'


def test_start_preflight_rejects_expired_manifest_before_shutdown(setup):
    m,c,host,calls,source,approved,manifest=setup
    request(c,'start',source,approved,manifest)
    original=host.run
    def run(argv,**kwargs):
        if 'app.tools.reorganization_worker' in argv and argv[-3]=='check-start':
            raise m.CommandFailed(2,'','STOP_JSON:'+json.dumps({'version':1,'error_code':'MANIFEST_EXPIRED','detail':'private /db/secret token=x'}))
        return original(argv,**kwargs)
    host.run=run
    host.tick()
    assert not any('compose' in a and 'stop' in a for a,_ in calls)
    result=status(c,source,'start')
    assert result['error_code']=='MANIFEST_EXPIRED' and 'vyprš' in result['detail']
    assert 'token' not in json.dumps(result)
    private=json.loads((c.private_dir/(source+'.start-error.json')).read_bytes())
    assert 'private /db/secret token=x' in private['stderr']


@pytest.mark.parametrize('code',['DATABASE_CHANGED','CAPABILITIES_UNVERIFIED','PLAN_UNRESOLVED'])
def test_preclaim_worker_failure_restores_app_only_with_positive_idle_proof(setup,code):
    m,c,host,calls,source,approved,manifest=setup
    request(c,'start',source,approved,manifest)
    original=host.run
    def run(argv,**kwargs):
        if 'app.tools.reorganization_worker' in argv and argv[-3]=='execute':
            raise m.CommandFailed(2,'','STOP_JSON:'+json.dumps({'version':1,'error_code':code,'detail':'private diagnostic'}))
        return original(argv,**kwargs)
    host.run=run
    host.tick()
    assert any('up' in a for a,_ in calls)
    data=status(c,source,'start')
    assert data['state']=='STOP' and data['phase']=='execution_not_started' and data['e7_started'] is False
    assert data['error_code']==code
    assert json.loads((c.service_status/'progress.json').read_bytes())['app_ready'] is True


def test_uncertain_worker_stop_or_idle_evidence_never_restarts_app(setup):
    m,c,host,calls,source,approved,manifest=setup
    request(c,'start',source,approved,manifest)
    original=host.run
    def run(argv,**kwargs):
        if 'app.tools.reorganization_worker' in argv and argv[-3] in {'execute','check-idle'}:
            raise RuntimeError('evidence uncertain')
        return original(argv,**kwargs)
    host.run=run
    host.tick()
    assert not any('up' in a for a,_ in calls)
    assert status(c,source,'start')['e7_started'] is None


def test_historical_requests_do_not_rewrite_claims_each_tick(setup,monkeypatch):
    m,c,host,*_=setup
    request(c)
    host.tick()
    original=m.write_json
    def write(path,*args,**kwargs):
        assert not str(path).endswith('-claim.json')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(m,'write_json',write)
    host.tick();host.tick()


def test_small_cleanup_status_and_on_demand_detail_cache_large_snapshot(setup,monkeypatch):
    m,c,host,_,source,approved,_=setup
    host.active_source=source
    record=proposal_record(source,approved)
    record['proposal']['before_snapshot']=[dict(path='Show/'+str(i)+'界'*65,kind='regular',device=1,inode=i,mode=33152,uid=1000,gid=1000,nlink=1,size=1024,mtime_ns=1794312345678901234,ctime_ns=1794312345678901234) for i in range(5901)]
    record['proposal']['candidates']=[dict(path='Old/'+str(i)+'界'*40,identity=[1,i],disposition='empty_after_actions') for i in range(279)]
    proposal=record['proposal'];proposal['proposal_sha256']=hashlib.sha256(encode({k:v for k,v in proposal.items() if k!='proposal_sha256'})).hexdigest()
    save(c.ledger/(source+'.cleanup-request.json'),record)
    original=m.read_json;reads=[]
    def read(path,*args,**kwargs):
        if str(path).endswith('.cleanup-request.json'):reads.append(path)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(m,'read_json',read)
    small,cookie=host.cleanup_view(None)
    assert len(encode(small))<2048 and small['candidate_count']==279 and small['proposal_sha256']==proposal['proposal_sha256']
    assert 'proposal' not in small and 'proposal_canonical' not in small
    for _ in range(3):host.cleanup_view(cookie)
    detail,_=host.cleanup_proposal(cookie,proposal['proposal_sha256'])
    assert detail['proposal']=={'proposal_sha256':proposal['proposal_sha256'],'candidates':proposal['candidates']}
    assert len(reads)==1
    assert len(encode(detail))<200000
    with pytest.raises(m.Conflict):host.cleanup_proposal(cookie,'0'*64)


def test_failed_cleanup_publication_becomes_explicit_stop(setup,monkeypatch):
    m,c,host,_,source,approved,_=setup
    host.active_source=source
    record=proposal_record(source,approved);save(c.ledger/(source+'.cleanup-request.json'),record)
    view,cookie=host.cleanup_proposal(None,record['proposal']['proposal_sha256'])
    body=dict(source_sha256=source,approved_sha256=approved,proposal_sha256=record['proposal']['proposal_sha256'],actor='Mirek',directories=[],confirm=True,csrf=view['csrf'])
    original=m.write_json
    def write(path,*args,**kwargs):
        if str(path).endswith('.cleanup-decision.json'):raise OSError('disk unavailable')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(m,'write_json',write)
    with pytest.raises(OSError):host.cleanup_decide(body,origin=c.public_origin,cookie=cookie)
    assert m.read_json(c.ledger/(source+'.cleanup-stop.json'))['reason']=='EVIDENCE_UNCERTAIN'
    state,_=host.cleanup_view(cookie)
    assert state['decision_state']=='STOP' and not state['waiting']


def test_runtime_capability_receipt_is_bound_to_live_mount_and_current_writer_fence(setup,monkeypatch):
    m,c,host,*_=setup
    # Static policy assertions cannot replace the bound runtime record.
    proof=m.read_json(c.runtime_evidence)
    with pytest.raises(ValueError):host.write_stage_runtime('a'*64,'plan')
    host.stop_writers()
    host.write_stage_runtime('a'*64,'plan')
    stage=m.read_json(c.ledger/('a'*64+'.plan-runtime.json'))
    assert stage['scanner_paused_through_verification'] is True
    assert stage['inventory_writer_paused_through_verification'] is True
    assert stage['mount_identity'] is None
    monkeypatch.setattr(m,'host_mount_identity',lambda *args:'remounted-identity')
    with pytest.raises(ValueError):host.runtime_proof()
    assert m.read_json(c.runtime_evidence)==proof


@pytest.mark.parametrize('flag',['mount_read_write_checked','safe_write_capability_proven','rename_noreplace_capability_proven'])
def test_missing_runtime_capability_is_rejected_before_shutdown(setup,flag):
    m,c,host,calls,*_=setup
    proof=m.read_json(c.runtime_evidence);proof[flag]=False;save(c.runtime_evidence,proof)
    request(c)
    host.tick()
    assert not calls
    assert status(c,'a'*64,'prepare')['error_code']=='CAPABILITIES_UNVERIFIED'


def test_host_mount_binding_changes_after_remount_or_reboot(setup,monkeypatch):
    m,c,_,*_=setup
    original=Path.read_text
    state={'mount':'42 1 0:1 / '+str(c.media_rw)+' rw - cifs '+c.nas_source+' rw,vers=3.1.1','boot':'boot-one'}
    def read(path,*args,**kwargs):
        if path==Path('/proc/self/mountinfo'):return state['mount']
        if path==Path('/proc/sys/kernel/random/boot_id'):return state['boot']
        if str(path).startswith('/proc/self/fdinfo/'):return 'mnt_id:\t'+state['mount'].split()[0]+'\n'
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    binding=module().host_mount_identity
    first=binding(c.media_rw,c.nas_source)
    state['mount']=state['mount'].replace('42 1','43 1')
    second=binding(c.media_rw,c.nas_source)
    assert first!=second
    state['boot']='boot-two'
    assert second!=binding(c.media_rw,c.nas_source)


def test_run_command_retains_only_bounded_private_diagnostics(setup,monkeypatch):
    m,*_=setup
    monkeypatch.setattr(m.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=2,stdout='O'*50000,stderr='private token='+'S'*50000))
    with pytest.raises(m.CommandFailed) as raised:m.run_command(['/fixed'])
    assert len(raised.value.stdout)==m.MAX_DIAGNOSTIC and len(raised.value.stderr)==m.MAX_DIAGNOSTIC
    assert 'token' not in str(raised.value)


def test_two_browser_cleanup_sessions_share_small_status_and_only_one_decision(setup):
    m,c,host,_,source,approved,_=setup
    host.active_source=source
    record=proposal_record(source,approved);save(c.ledger/(source+'.cleanup-request.json'),record)
    state_one,cookie_one=host.cleanup_view(None)
    state_two,cookie_two=host.cleanup_view(None)
    assert cookie_one!=cookie_two and state_one==state_two
    one,_=host.cleanup_proposal(cookie_one,state_one['proposal_sha256'])
    two,_=host.cleanup_proposal(cookie_two,state_two['proposal_sha256'])
    body=dict(source_sha256=source,approved_sha256=approved,proposal_sha256=state_one['proposal_sha256'],actor='Mirek',directories=[],confirm=True,csrf=one['csrf'])
    host.cleanup_decide(body,origin=c.public_origin,cookie=cookie_one)
    with pytest.raises(m.Conflict):host.cleanup_decide({**body,'csrf':two['csrf']},origin=c.public_origin,cookie=cookie_two)
    state,_=host.cleanup_view(cookie_two)
    assert not state['waiting'] and state['decision_state']=='CONSUMED'


def test_corrupt_cleanup_evidence_signals_stop_instead_of_waiting(setup):
    m,c,host,_,source,*_=setup
    host.active_source=source
    path=c.ledger/(source+'.cleanup-request.json')
    path.write_bytes(b'{broken')
    with pytest.raises(ValueError):host.cleanup_view(None)
    assert m.read_json(c.ledger/(source+'.cleanup-stop.json'))==dict(version=1,source_sha256=source,reason='EVIDENCE_UNCERTAIN')


def test_cleanup_stop_marker_failure_stops_only_active_fixed_worker(setup,monkeypatch):
    m,c,host,calls,source,*_=setup
    host.active_source=source
    original=m.write_json
    def no_marker(path,*args,**kwargs):
        if str(path).endswith('.cleanup-stop.json'):raise OSError('no evidence space')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(m,'write_json',no_marker)
    host.stop_cleanup(source)
    assert calls[-1][0]==['/usr/bin/docker','stop','--time=10',host.container_name(source,'start','execute')]


def test_mount_capability_binding_uses_live_descriptor_not_hidden_old_mount(setup,monkeypatch):
    _,c,*_=setup
    m=module()
    original=Path.read_text
    raw=f'42 1 0:1 / {c.media_rw} rw - cifs {c.nas_source} rw,vers=3.1.1\n43 1 0:1 / {c.media_rw} rw - cifs {c.nas_source} rw,vers=3.1.1'
    def read(path,*args,**kwargs):
        if path==Path('/proc/self/mountinfo'):return raw
        if str(path).startswith('/proc/self/fdinfo/'):return 'mnt_id:\t43\n'
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    binding=m.host_mount_identity(c.media_rw,c.nas_source)
    assert ':43:' in binding and ':42:' not in binding


@pytest.mark.parametrize('startup', ['slow', 'exited', 'restarting', 'timeout'])
def test_restart_wait_uses_health_and_runtime_without_repeating_execution(setup, monkeypatch, startup):
    m, c, host, calls, source, approved, manifest = setup
    request(c, 'start', source, approved, manifest)
    elapsed = [0.0]
    monkeypatch.setattr(m.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(m.time, 'sleep', lambda seconds: elapsed.__setitem__(0, elapsed[0]+seconds))
    host.healthy = lambda url: startup=='slow' and elapsed[0]>=180
    original = host.run
    def runtime(argv, **kwargs):
        if 'ps' in argv and '--all' in argv:
            return 'f'*64
        if argv[:3] == ['/usr/bin/docker','inspect','--format']:
            return json.dumps(dict(Status='exited' if startup=='exited' else 'running',
                Running=startup!='exited', Restarting=startup=='restarting', Dead=False,
                Paused=False, OOMKilled=False, ExitCode=1 if startup=='exited' else 0, Error=''))
        return original(argv, **kwargs)
    host.run = runtime
    host.tick()
    data = status(c, source, 'start')
    progress = json.loads((c.service_status/'progress.json').read_bytes())
    assert data['state']=='NORMAL' and progress['outcome']=='NORMAL'
    assert [a[-3] for a, _ in calls if 'app.tools.reorganization_worker' in a]==['check-start','execute']
    if startup=='slow':
        assert data['phase']=='normal' and progress['app_ready'] is True
        assert elapsed[0]==180 and data['app_restart_state']=='available'
    else:
        assert data['phase']=='app_restart_failed' and progress['app_ready'] is False
        assert data['app_restart_state']==('timed_out' if startup=='timeout' else 'failed')
        assert elapsed[0]==(900 if startup=='timeout' else 0)
        assert not any(a[0]==str(c.writers_start_hook) for a, _ in calls)
    before = list(calls); host.tick(); assert calls==before


def test_restart_budget_can_be_configured_and_rejects_invalid_values(setup):
    from dataclasses import replace
    m, c, *_ = setup
    for value in (0, -1, True, 3601, float('nan'), '900'):
        with pytest.raises(ValueError): replace(c, app_health_timeout_seconds=value)
    configured = replace(c, app_health_timeout_seconds=600)
    host = m.Coordinator(configured, run=lambda *a, **k: '', healthy=lambda _:False)
    assert host.health_timeout==600


def test_host_accepts_exact_subs_cleanup_but_refuses_retained_child(setup):
    m, c, host, _, source, approved, _ = setup
    host.active_source=source
    record=proposal_record(source, approved)
    record['proposal']['candidates']=[dict(path=p,identity=[1,i+2],disposition='REVIEW')
        for i,p in enumerate(('Subs','Subs/empty'))]
    record['proposal']['before_snapshot']=[dict(path=p,kind='directory') for p in ('Subs','Subs/empty')]
    record['proposal']['proposal_sha256']=hashlib.sha256(encode({k:v for k,v in record['proposal'].items() if k!='proposal_sha256'})).hexdigest()
    save(c.ledger/(source+'.cleanup-request.json'),record)
    view,cookie=host.cleanup_proposal(None, record['proposal']['proposal_sha256'])
    data=dict(source_sha256=source,approved_sha256=approved,proposal_sha256=record['proposal']['proposal_sha256'],
        actor='operator',directories=['Subs'],confirm=True,csrf=view['csrf'])
    with pytest.raises(ValueError,match='retained children'):
        host.cleanup_decide(data,origin=c.public_origin,cookie=cookie)
    data['directories']=['Subs/empty','Subs']
    host.cleanup_decide(data,origin=c.public_origin,cookie=cookie)
    assert json.loads((c.ledger/(source+'.cleanup-decision.json')).read_bytes())['directories']==['Subs/empty','Subs']


def test_health_arriving_after_deadline_is_not_accepted(setup, monkeypatch):
    m, c, host, calls, *_ = setup
    elapsed=[0.0]
    monkeypatch.setattr(m.time,'monotonic',lambda:elapsed[0])
    monkeypatch.setattr(m.time,'sleep',lambda seconds:elapsed.__setitem__(0,elapsed[0]+seconds))
    host.health_timeout=2
    host.healthy=lambda _:elapsed[0]>=3
    original=host.run
    def delayed(argv, **kwargs):
        if 'ps' in argv and '--all' in argv:
            elapsed[0]+=3
            return 'f'*64
        if 'inspect' in argv:
            return json.dumps(dict(Status='running',Running=True,Restarting=False,Dead=False,Paused=False,OOMKilled=False,Error=''))
        return original(argv, **kwargs)
    host.run=delayed
    with pytest.raises(TimeoutError):host.restart_app()
    assert host.app_restart_state=='timed_out'
    assert not any(a[0]==str(c.writers_start_hook) for a,_ in calls)


@pytest.mark.parametrize('budget,expected', [(2,'timed_out'), (900,'failed')])
def test_real_command_timeout_distinguishes_runtime_failure_from_maximum_wait(setup, monkeypatch, budget, expected):
    m, _, host, _, *_ = setup
    elapsed=[0.0]
    monkeypatch.setattr(m.time,'monotonic',lambda:elapsed[0])
    host.health_timeout=budget
    host.healthy=lambda _:False
    original=host.run
    def subprocess_timeout(argv, **kwargs):
        elapsed[0]+=kwargs['timeout']
        raise m.subprocess.TimeoutExpired(argv,kwargs['timeout'])
    monkeypatch.setattr(m.subprocess,'run',subprocess_timeout)
    def run(argv, **kwargs):
        if 'ps' in argv and '--all' in argv:return m.run_command(argv,**kwargs)
        return original(argv,**kwargs)
    host.run=run
    with pytest.raises(m.CommandFailed):host.restart_app()
    assert host.app_restart_state==expected


@pytest.mark.parametrize('startup', ['slow','exited','timeout'])
def test_prepare_reports_restart_state_and_does_not_repeat_failed_startup(setup, monkeypatch, startup):
    m,c,host,calls,source,*_=setup
    data=request(c)
    elapsed=[0.0]
    monkeypatch.setattr(m.time,'monotonic',lambda:elapsed[0])
    monkeypatch.setattr(m.time,'sleep',lambda seconds:elapsed.__setitem__(0,elapsed[0]+seconds))
    def healthy(_):
        progress=json.loads((c.service_status/'progress.json').read_bytes())
        assert progress['app_restart_state']=='initializing' and progress['active'] is True
        return startup=='slow' and elapsed[0]>=180
    host.healthy=healthy
    original=host.run
    def run(argv,**kwargs):
        if 'ps' in argv and '--all' in argv:return 'f'*64
        if 'inspect' in argv:return json.dumps(dict(Status='exited' if startup=='exited' else 'running',
            Running=startup!='exited',Restarting=False,Dead=False,Paused=False,OOMKilled=False,Error=''))
        return original(argv,**kwargs)
    host.run=run
    host.tick()
    result=status(c,data['request_id'],'prepare')
    progress=json.loads((c.service_status/'progress.json').read_bytes())
    expected={'slow':'available','exited':'failed','timeout':'timed_out'}[startup]
    assert result['app_restart_state']==progress['app_restart_state']==expected
    assert result['state']==('READY' if startup=='slow' else 'STOP')
    assert sum('up' in argv for argv,_ in calls)==1
    assert not any('app.tools.reorganization_worker' in a and a[-3]=='execute' for a,_ in calls)
