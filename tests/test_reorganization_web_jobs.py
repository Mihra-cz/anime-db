"""Browser requests delegate to the host without acquiring execution authority."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from test_reorganization_approval_web import web, case, production, csrf, prepare
from test_reorganization_approval import digest, snapshot, accept


ORIGIN = {'origin': 'http://testserver'}


def prepare_fields(page):
    form = re.search(r'<form[^>]+id="reorganization-prepare"[^>]*>(.*?)</form>', page.text, re.S)
    assert form, page.text[:5000]
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form[1]))


def preview(web):
    _, client, _ = web
    page = client.post('/reorganization/preview')
    assert page.status_code == 200, page.text
    return page, prepare_fields(page)


def approve(web):
    case, client, source = web
    path, page, final = prepare(web)
    page = client.post(path + '/approve', data={'csrf': csrf(page), 'sha256': digest(final),
        'actor': 'operator', 'confirm': 'yes'}, headers=ORIGIN)
    assert page.status_code == 200
    return path, page, final


def test_preview_produces_signed_create_once_prepare_request(web):
    case, client, _ = web
    before = snapshot(case[2], case[1].root)
    _, fields = preview(web)
    fields['actor'] = 'operator'
    response = client.post('/reorganization/prepare', data=fields, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    request_id = response.headers['location'].rsplit('/', 1)[1]
    assert re.fullmatch('[a-f0-9]{64}', request_id)
    record = json.loads(case[0].read_record(case[6], request_id + '.prepare-request.json'))
    assert set(record) == {'version', 'kind', 'request_id', 'actor', 'requested_at', 'preview_db_sha256'}
    assert record['kind'] == 'prepare' and record['request_id'] == request_id and record['actor'] == 'operator'
    assert record['preview_db_sha256'] == hashlib.sha256(case[2].read_bytes()).hexdigest()
    assert client.post('/reorganization/prepare', data=fields, headers=ORIGIN).status_code == 409
    page = client.get(response.headers['location'])
    assert 'Čeká na hostitelský koordinátor' in page.text
    assert 'Připraveno ke schválení' not in page.text
    assert snapshot(case[2], case[1].root) == before


@pytest.mark.parametrize('fault', ['missing', 'csrf', 'sha', 'stale', 'origin'])
def test_prepare_rejects_unsigned_stale_or_cross_origin_preview(web, fault):
    case, client, _ = web
    _, fields = preview(web)
    fields['actor'] = 'operator'
    headers = ORIGIN
    if fault == 'missing': fields = {'actor': 'operator'}
    elif fault == 'csrf': fields['csrf'] = 'fake'
    elif fault == 'sha': fields['preview_db_sha256'] = '0' * 64
    elif fault == 'stale':
        with sqlite3.connect(case[2]) as connection:
            connection.execute('PRAGMA user_version=18')
    else: headers = {'origin': 'https://other.example'}
    response = client.post('/reorganization/prepare', data=fields, headers=headers)
    assert response.status_code in (403, 409, 422)
    assert not list(case[6].glob('*.prepare-request.json'))


@pytest.mark.parametrize('fault', ['unknown', 'blocked', 'incomplete'])
def test_invalid_preview_has_no_prepare_capability(web, fault):
    case, client, _ = web
    if fault == 'unknown': (case[1].root / 'unknown.mkv').write_bytes(b'unknown')
    elif fault == 'blocked':
        target = case[1].root / case[3].payload.actions[0].target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'collision')
    else:
        (case[1].root / case[3].payload.actions[0].source).unlink()
    page = client.post('/reorganization/preview')
    assert page.status_code == 200
    assert 'id="reorganization-prepare"' not in page.text


def test_job_status_get_reads_only_bound_host_status(web, monkeypatch):
    from app import reorganization_web as module
    case, client, source = web
    _, fields = preview(web)
    response = client.post('/reorganization/prepare', data={**fields, 'actor': 'operator'}, headers=ORIGIN, follow_redirects=False)
    request_id = response.headers['location'].rsplit('/', 1)[1]
    monkeypatch.setattr(module, 'run_preview', lambda *a, **k: pytest.fail('GET started inventory'))
    before = snapshot(case[2], case[1].root)
    assert client.get(response.headers['location']).status_code == 200
    case[0].write_record(case[5], request_id + '.prepare-status.json', json.dumps({
        'version': 1, 'kind': 'prepare', 'request_id': request_id, 'state': 'READY',
        'phase': 'manifest', 'detail': 'Manifest připraven', 'source_sha256': source}))
    state = client.get(response.headers['location'] + '/status').json()
    assert state['state'] == 'READY' and state['result_url'] == '/reorganization/approval/' + source
    assert snapshot(case[2], case[1].root) == before


def test_explicit_start_creates_exact_request_once_and_uses_stable_edge_page(web):
    case, client, source = web
    path, page, final = approve(web)
    assert f'action="{path}/start"' in page.text
    before = snapshot(case[2], case[1].root)
    fields = {'csrf': csrf(page), 'sha256': digest(final), 'actor': 'operator', 'confirm': 'yes'}
    response = client.post(path + '/start', data=fields, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/_service/workflow?source=' + source
    record = json.loads(case[0].read_record(case[6], source + '.start-request.json'))
    assert record == {'version': 1, 'kind': 'start', 'request_id': source, 'source_sha256': source,
        'approved_sha256': digest(final), 'manifest_id': final.manifest_id,
        'actor': 'operator', 'requested_at': record['requested_at']}
    assert client.post(path + '/start', data=fields, headers=ORIGIN).status_code == 409
    assert snapshot(case[2], case[1].root) == before


def test_manifest_links_real_authority_editors_and_describes_warning(web, tmp_path):
    case, client, source = web
    page = client.get('/reorganization/approval/' + source)
    for path in ('/naming-review', '/naming-review/layout', '/hierarchy-review'):
        assert f'href="{path}"' in page.text
    chromium = shutil.which('chromium')
    if not chromium:
        pytest.skip('Chromium not installed')
    root = Path(__file__).resolve().parents[1]
    script = (root / 'app/static/reorganization_approval.js').read_text()
    html = re.sub(r'<script src="[^"]*reorganization_approval.js" defer></script>', '<script>' + script + '</script>', page.text)
    helper = (root / 'app/static/reorganization_presentation.js').read_text()
    html = re.sub(r'<script src="[^"]*reorganization_presentation.js" defer></script>', lambda _: '<script>'+helper+'</script>', html)
    html = html.replace('</body>', '''<script>
document.body.dataset.test=document.getElementById('manifest-warnings').innerText.includes('Titulky bez odpovídajícího videa')?'PASS':'warning description absent';
</script></body>''')
    local = tmp_path / 'manifest.html'; local.write_text(html)
    result = subprocess.run([chromium, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
        '--virtual-time-budget=500', '--dump-dom', local.as_uri()], capture_output=True, text=True, timeout=20)
    assert 'data-test="PASS"' in result.stdout, result.stdout[-1000:]


@pytest.mark.parametrize('fault', ['unapproved', 'sha', 'actor', 'confirm', 'csrf', 'spent', 'stale'])
def test_start_requires_fresh_exact_unspent_approval(web, fault):
    case, client, source = web
    if fault == 'unapproved': path, page, final = prepare(web)
    else: path, page, final = approve(web)
    fields = {'csrf': csrf(page), 'sha256': digest(final), 'actor': 'operator', 'confirm': 'yes'}
    if fault == 'sha': fields['sha256'] = '0' * 64
    elif fault == 'actor': fields['actor'] = 'other'
    elif fault == 'confirm': fields.pop('confirm')
    elif fault == 'csrf': fields['csrf'] = 'fake'
    elif fault == 'spent': accept(case, source, final)
    elif fault == 'stale':
        with sqlite3.connect(case[2]) as connection: connection.execute('PRAGMA user_version=18')
    assert client.post(path + '/start', data=fields, headers=ORIGIN).status_code in (403, 409, 422)
    assert not list(case[6].glob('*.start-request.json'))


def test_edge_has_independent_workflow_and_narrow_socket_mount():
    assets = Path(__file__).resolve().parents[1] / 'deploy/nexus/service-edge'
    nginx = (assets / 'nginx.conf').read_text()
    assert 'location = /_service/workflow' in nginx
    assert 'location = /_service/control/cleanup' in nginx
    assert 'unix:/run/animedb-control/coordinator.sock:/cleanup' in nginx
    compose = (assets / 'compose.edge.yaml').read_text()
    assert 'source: /run/animedb-reorganization' in compose
    assert 'target: /run/animedb-control' in compose
    assert '/var/run/docker.sock' not in compose


@pytest.mark.parametrize('decision', ['preserve', 'delete', 'stop'])
def test_edge_cleanup_browser_requires_explicit_exact_decision(tmp_path, decision):
    chromium = shutil.which('chromium')
    if not chromium:
        pytest.skip('Chromium not installed')
    assets = Path(__file__).resolve().parents[1] / 'deploy/nexus/service-edge'
    html = (assets / 'index.html').read_text().replace('<script defer src="/_service/service.js"></script>', '')
    script = (assets / 'service.js').read_text()
    harness = '''
const sent = [];let consumed=false;
const cleanup = {waiting:true,source_sha256:'a'.repeat(64),approved_sha256:'b'.repeat(64),
 csrf:'token',proposal:{proposal_sha256:'c'.repeat(64),candidates:[{path:'old',disposition:'REVIEW'},...Array.from({length:65},(_,i)=>({path:`old/${i}`,disposition:'CLEANUP_REVIEW'}))]}};
window.setInterval = () => 0;
window.fetch = async (url, options={}) => {
 if(options.method === 'POST') {sent.push(JSON.parse(options.body));consumed=true;return {ok:true,json:async()=>({accepted:true})};}
 if(url.includes('/control/cleanup/proposal?')) return {ok:true,json:async()=>cleanup};
 if(url.includes('/control/cleanup')) return {ok:true,json:async()=>({waiting:!consumed,source_sha256:cleanup.source_sha256,approved_sha256:cleanup.approved_sha256,proposal_sha256:cleanup.proposal.proposal_sha256,candidate_count:cleanup.proposal.candidates.length,decision_state:consumed?'CONSUMED':'WAITING',phase:'cleanup'})};
 return {ok:false};
};
'''
    checks = '''
(async()=>{try {
const check=(condition,message)=>{if(!condition)throw Error(message)};
const wait=async(test,message)=>{for(let i=0;i<200;i++){if(test())return;await new Promise(resolve=>setTimeout(resolve,10));}throw Error(message)};
const panel=document.getElementById('cleanup-panel');
await wait(()=>panel && !panel.hidden,'cleanup candidates absent');
check(panel.querySelectorAll('#cleanup-candidates input:checked').length===50,'first page not preselected');
check(!document.getElementById('cleanup-confirm').checked,'preselection confirmed deletion');
check(sent.length===0,'automatic decision');
document.getElementById('cleanup-next').click();
check(panel.querySelectorAll('#cleanup-candidates input:checked').length===16,'next page not preselected');
document.getElementById('cleanup-actor').value='Test operator';
document.getElementById('cleanup-search').value='old/64';
document.getElementById('cleanup-search').dispatchEvent(new Event('input'));
const choice=panel.querySelector('[data-cleanup-path="old/64"]');
check(choice && choice.checked,'search lost default selection');choice.click();
document.getElementById('cleanup-search').value='';
document.getElementById('cleanup-search').dispatchEvent(new Event('input'));
check(!panel.querySelector('[data-cleanup-path="old"]').checked,'retained child did not retain parent');
document.getElementById('cleanup-next').click();
check(!panel.querySelector('[data-cleanup-path="old/64"]').checked,'page/filter reset retained child');
check(document.getElementById('cleanup-selected').textContent.includes('Odstranit: 64 · zachovat: 2'),'selection summary differs');
'''
    if decision == 'delete':
        checks += '''document.getElementById('cleanup-delete').click();
check(sent.length===0,'delete accepted without exact SHA and confirmation');
document.getElementById('cleanup-sha').value='d'.repeat(64);
document.getElementById('cleanup-confirm').checked=true;
document.getElementById('cleanup-delete').click();
check(sent.length===0,'delete accepted with wrong SHA');
document.getElementById('cleanup-sha').value='c'.repeat(64);
document.getElementById('cleanup-confirm').checked=false;
document.getElementById('cleanup-delete').click();
check(sent.length===0,'delete accepted without confirmation');
document.getElementById('cleanup-confirm').checked=true;
document.getElementById('cleanup-delete').click();'''
    else:
        checks += "document.getElementById('cleanup-" + ('preserve' if decision == 'preserve' else 'stop') + "').click();"
    checks += '''
await wait(()=>sent.length===1,'explicit decision not sent');
check(sent.length===1,'explicit decision not sent exactly once');
const body=sent[0];check(body.source_sha256==='a'.repeat(64)&&body.approved_sha256==='b'.repeat(64)&&body.proposal_sha256==='c'.repeat(64),'identity differs');
check(body.actor==='Test operator'&&body.csrf==='token','actor or CSRF missing');
check(JSON.stringify(Object.keys(body).sort())===JSON.stringify(['source_sha256','approved_sha256','proposal_sha256','actor','csrf','directories','confirm',...(body.decision?['decision']:[])].sort()),'unexpected or missing decision fields');
'''
    if decision == 'stop': checks += "check(body.decision==='stop'&&body.confirm===true&&Array.isArray(body.directories)&&body.directories.length===0,'STOP payload differs');"
    elif decision == 'preserve': checks += "check(body.confirm===true&&body.directories.length===0,'keep-all did not preserve');"
    else: checks += "check(body.confirm===true&&JSON.stringify(body.directories)===JSON.stringify(Array.from({length:64},(_,i)=>'old/'+i).sort()),'submitted selection lost retained child/parent or other pages');"
    checks += "document.body.dataset.test='PASS';}catch(error){document.body.dataset.test=error.message}})();"
    page = tmp_path / 'cleanup.html'
    page.write_text(html.replace('</body>', '<script>' + harness + script + checks + '</script></body>'))
    result = subprocess.run([chromium, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
        '--virtual-time-budget=3500', '--dump-dom', page.as_uri()], capture_output=True, text=True, timeout=20)
    assert 'data-test="PASS"' in result.stdout, result.stdout[-6000:] + result.stderr[-1000:]


@pytest.mark.parametrize('scenario,expected', [
    ('prepare_ready', 'RETURNED'), ('prepare_stop', 'RETURNED'), ('normal', 'RETURNED'),
    ('health_only', 'STAY'), ('readiness_only', 'STAY'), ('wrong_source', 'STAY'), ('start_unverified', 'STAY'),
    ('execution_not_started', 'RETURNED'),
])
def test_edge_returns_only_to_bound_result_after_verified_restart(scenario, expected):
    chromium = shutil.which('chromium')
    if not chromium:
        pytest.skip('Chromium not installed')
    assets = Path(__file__).resolve().parents[1] / 'deploy/nexus/service-edge'
    source, request_id = 'a' * 64, 'b' * 64
    prepare_job = scenario.startswith('prepare')
    initial = '/reorganization/jobs/' + request_id if prepare_job else '/_service/workflow?source=' + source
    target = '/reorganization/jobs/' + request_id if scenario == 'prepare_stop' else '/reorganization/approval/' + source
    from time import time
    state = {'version': 1, 'run_id': 'c' * 32, 'active': False, 'updated_at': time(),
        'outcome': 'UNVERIFIED' if scenario in {'prepare_ready', 'start_unverified'} else 'STOP' if scenario in {'prepare_stop','execution_not_started'} else 'NORMAL',
        'completed': 0, 'rename_completed': 0, 'total': 0, 'app_ready': scenario != 'health_only',
        'source_sha256': 'd' * 64 if scenario == 'wrong_source' else source,
        'result_url': target, 'stage': 'ready' if scenario in {'prepare_ready', 'start_unverified'} else 'execution_not_started' if scenario == 'execution_not_started' else 'stopped',
        'detail': 'Ověřený restart', 'phases': [{'id': name, 'state': 'PASS'} for name in
            ('manifest', 'folders', 'rename', 'sqlite', 'cleanup', 'verification')]}
    page = (assets / 'index.html').read_text().replace('</body>', "<script>setTimeout(()=>document.body.dataset.test='STAY',400)</script></body>")
    class Handler(BaseHTTPRequestHandler):
        initial_visits = 0
        def do_GET(self):
            if self.path == initial and self.initial_visits == 0:
                type(self).initial_visits += 1
                body, code, content_type = page, 200, 'text/html'
            elif self.path == target:
                body, code, content_type = '<html><body data-test="RETURNED">Výsledek</body></html>', 200, 'text/html'
            elif self.path == '/_service/service.js':
                body, code, content_type = (assets / 'service.js').read_text(), 200, 'application/javascript'
            elif self.path == '/_service/progress.json':
                body, code, content_type = json.dumps(state), 200, 'application/json'
            elif self.path == '/_service/control/cleanup':
                body, code, content_type = '{"waiting":false}', 200, 'application/json'
            elif self.path == '/_service/app-health':
                body, code, content_type = 'ok', 503 if scenario == 'readiness_only' else 200, 'text/plain'
            else:
                body, code, content_type = '', 404, 'text/plain'
            self.send_response(code); self.send_header('Content-Type', content_type); self.end_headers()
            self.wfile.write(body.encode())
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        response = subprocess.run([chromium, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
            '--virtual-time-budget=1500', '--dump-dom', f'http://127.0.0.1:{server.server_port}' + initial],
            capture_output=True, text=True, timeout=20)
        assert f'data-test="{expected}"' in response.stdout, response.stdout[:3500] + response.stderr[-1000:]
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
