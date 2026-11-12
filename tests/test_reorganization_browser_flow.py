"""Browser → coordinator → real E7/E7H → NORMAL with only synthetic local media.

Only deployment boundaries (Docker/Compose and Synology snapshot) are simulated.
The planner, canonical approval, filesystem actions, SQLite and cleanup are real.
"""
import http.client
from dataclasses import asdict
from types import SimpleNamespace
import json
import os
import socket
import threading
import time

import pytest

from app import reorganization_approval as approval
from app.tools import reorganization_worker as worker
from test_reorganization_approval_web import web, case, production, csrf
from test_reorganization_web_jobs import prepare_fields, ORIGIN
from test_reorganization_worker import config_for
from test_reorganization_coordinator import module


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('testserver', timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self.path))


@pytest.mark.parametrize('cleanup_choice', ['keep', 'selected', 'stop', 'before_claim'])
def test_browser_can_prepare_approve_start_and_finish_original_e7(web, tmp_path, monkeypatch, cleanup_choice):
    case, client, _ = web
    config = config_for(case)
    from v6_database_case import database_rows
    rows = database_rows(config.database)
    page = client.post('/reorganization/preview')
    assert page.status_code == 200
    request_page = client.post('/reorganization/prepare', data={**prepare_fields(page), 'actor': 'operator'},
        headers=ORIGIN, follow_redirects=False)
    assert request_page.status_code == 303
    request_id = request_page.headers['location'].rsplit('/', 1)[1]
    host_module = module()
    dirs = {}
    for name in ('private_dir', 'control_dir', 'state_dir', 'media_ro'):
        dirs[name] = tmp_path / name
        dirs[name].mkdir(mode=0o700)
    host_config = host_module.Config(version=1, image='anime-db:v6-0123456789ab', uid=os.getuid(),gid=os.getgid(),
        public_origin='http://testserver', compose_files=tuple(tmp_path / f'compose-{i}.yaml' for i in range(3)),
        worker_config=tmp_path / 'worker.json', snapshot_hook=tmp_path / 'snapshot',
        writers_stop_hook=tmp_path / 'stop-writers', writers_start_hook=tmp_path / 'start-writers',
        socket_gid=os.getgid(), nas_source=config.share, runtime_evidence=tmp_path/'runtime-evidence.json', database_dir=config.database.parent,
        media_rw=config.library_root, inbox=config.inbox,outbox=config.outbox,ledger=config.ledger,
        backups=config.backups,service_status=config.service,**dirs)
    monkeypatch.setattr(host_module, 'require_rw_mount', lambda *args: None)
    monkeypatch.setattr(host_module, 'host_mount_identity', lambda *args: 'fixture-host-mount')
    monkeypatch.setattr(host_module, 'resolve_deployment', lambda files: SimpleNamespace(files=files,env_file=tmp_path/'.env',project_directory=tmp_path,project_name='anime-db'))
    proof=asdict(case[4]);proof['mount_identity']='fixture-host-mount'
    host_config.runtime_evidence.write_text(json.dumps(proof))
    application = {'running': True}
    calls = []
    def runtime(argv, *, input=None, timeout=120):
        calls.append(argv)
        if 'config' in argv: return host_config.image
        if 'compose' in argv and 'ps' in argv: return 'app' if application['running'] else ''
        if 'compose' in argv and 'stop' in argv:
            application['running'] = False
        if 'compose' in argv and 'up' in argv:
            application['running'] = True
        if argv[0] == str(host_config.writers_stop_hook): return '{"writers_stopped":true}'
        if argv[0] == str(host_config.writers_start_hook): return '{"writers_started":true}'
        if argv[0] == str(host_config.snapshot_hook):
            binding = json.loads(input)
            return json.dumps(dict(snapshot_id='isolated-snapshot',snapshot_at=approval.now(),
                snapshot_manifest_id=binding['manifest_id']))
        if 'app.tools.reorganization_worker' in argv:
            verb = argv[-3]
            if verb in {'check-start','check-idle'}:
                return json.dumps((worker.check_start if verb=='check-start' else worker.check_idle)(config,argv[-1]))
            assert not application['running']
            if cleanup_choice=='before_claim' and verb=='execute':
                import sqlite3
                with sqlite3.connect(config.database) as connection:
                    connection.execute("UPDATE videos SET filename=filename || ' edited' WHERE id=100")
                try: worker.execute(config,argv[-1])
                except Exception as error:
                    raise host_module.CommandFailed(2,'','STOP_JSON:'+json.dumps(dict(version=1,
                        error_code=worker.error_code(error),detail=str(error)))) from error
                raise AssertionError('changed DB was executed')
            result = worker.publish(config, request_id, json.loads(input)) if verb == 'publish' else (
                worker.plan(config, request_id) if verb == 'plan' else worker.execute(config, argv[-1]))
            return json.dumps(result)
        return ''
    host = host_module.Coordinator(host_config, run=runtime, healthy=lambda _: application['running'])
    host.tick()
    state = client.get(request_page.headers['location'] + '/status').json()
    assert state['state'] == 'READY', state
    assert application['running']
    assert 'execute' not in [c[-3] for c in calls if 'app.tools.reorganization_worker' in c]
    source = state['source_sha256']
    path = state['result_url']
    bundle = approval.read_proposal(config.inbox, source)
    original = approval.manifest_from_json(bundle['manifest'])
    page = client.get(path)
    page = client.post(path + '/prepare', data={'csrf':csrf(page),'actor':'operator',
        'warnings':[w.warning_id for w in original.payload.warnings]}, headers=ORIGIN)
    assert page.status_code == 200, page.text
    final = approval.read_review(config.outbox,source)
    assert final.acknowledgements and approval.sha256(final) != source
    page = client.post(path + '/approve',data={'csrf':csrf(page),'actor':'operator','sha256':approval.sha256(final),
        'confirm':'yes'},headers=ORIGIN)
    assert page.status_code == 200, page.text
    assert not list(config.ledger.glob('*.accepted.json'))
    response = client.post(path + '/start',data={'csrf':csrf(page),'actor':'operator','sha256':approval.sha256(final),
        'confirm':'yes'},headers=ORIGIN,follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'].startswith('/_service/workflow')
    case[1].close()
    server = host_module.make_control_server(host)
    serving = threading.Thread(target=server.serve_forever,daemon=True)
    serving.start()
    running = threading.Thread(target=host.tick,daemon=True)
    running.start()
    if cleanup_choice=='before_claim':
        try:
            running.join(35)
            assert not running.is_alive() and application['running']
            status=json.loads((config.inbox/(source+'.start-status.json')).read_text())
            assert status['state']=='STOP' and status['phase']=='execution_not_started'
            assert status['e7_started'] is False and status['error_code']=='DATABASE_CHANGED'
            assert not list(config.ledger.glob('*.accepted.json'))
            assert not config.database.with_suffix('.journal.jsonl').exists()
            assert not (config.inbox/(source+'.result.json')).exists()
            report=client.get(path)
            assert report.status_code==200 and 'E7 nezačalo' in report.text
            assert 'Požadavek čeká na ověření hostitelem' not in report.text
        finally:
            server.shutdown();server.server_close();serving.join(5)
        return
    try:
        deadline=time.monotonic()+35
        live=None
        while time.monotonic()<deadline and running.is_alive():
            connection=UnixConnection(host_config.control_dir/'coordinator.sock')
            try:
                connection.request('GET','/cleanup')
                reply=connection.getresponse()
                view=json.loads(reply.read())
                cookie=reply.getheader('Set-Cookie')
            finally: connection.close()
            if view.get('waiting'):
                connection=UnixConnection(host_config.control_dir/'coordinator.sock')
                try:
                    connection.request('GET','/cleanup/proposal?sha='+view['proposal_sha256'],headers={'Cookie':cookie.split(';',1)[0]})
                    reply=connection.getresponse();live=json.loads(reply.read())
                finally:connection.close()
                break
            time.sleep(.03)
        assert live is not None
        assert not application['running'] and running.is_alive()
        progress=json.loads((config.service/'progress.json').read_text())
        assert progress['waiting_for_operator'] and progress['source_sha256']==source
        # The host control server remains available after the ordinary app stops.
        selected=[p['path'] for p in live['proposal']['candidates'] if p['path'].startswith('old')] if cleanup_choice=='selected' else []
        data=dict(source_sha256=source,approved_sha256=approval.sha256(final),
            proposal_sha256=live['proposal']['proposal_sha256'],actor='browser operator',directories=selected,
            confirm=True,csrf=live['csrf'])
        if cleanup_choice=='stop': data['decision']='stop'
        connection=UnixConnection(host_config.control_dir/'coordinator.sock')
        try:
            connection.request('POST','/cleanup',body=json.dumps(data),headers={
                'Origin':'http://testserver','Content-Type':'application/json','Cookie':cookie.split(';',1)[0]})
            reply=connection.getresponse()
            assert reply.status==200, reply.read()
            reply.read()
        finally: connection.close()
        running.join(35)
        assert not running.is_alive()
        result=approval.read_execution_result(config.inbox,source,final)
        if cleanup_choice=='stop':
            assert result['state']=='STOP' and not application['running']
            assert config.database.with_suffix('.journal.jsonl').exists()
            assert config.database.with_name(config.database.name+'.execution-guard.json').exists()
        else:
            assert result['state']=='NORMAL' and application['running']
            assert all(not (config.library_root / p).exists() for p in selected)
            progress=json.loads((config.service/'progress.json').read_text())
            assert progress['outcome']=='NORMAL' and progress['app_ready']
            from fastapi.testclient import TestClient
            from app.config import Settings
            from app.main import create_app
            settings=Settings(anime_path=config.library_root,database_url=f'sqlite:///{config.database}',
                metadata_artwork_directory=tmp_path/'artwork',metadata_download_artwork=False,
                reorganization_approval_inbox=config.inbox,reorganization_approval_outbox=config.outbox)
            with TestClient(create_app(settings)) as restarted:
                report=restarted.get(path)
                assert report.status_code==200 and 'Výsledek reorganizace' in report.text and 'NORMAL' in report.text
            from test_v6_execution_projection import expected_rows
            assert database_rows(config.database)==expected_rows(rows,final.payload.locator_patches)
        call_count=len(calls)
        host.tick()
        assert len(calls)==call_count
    finally:
        server.shutdown();server.server_close();serving.join(5)
