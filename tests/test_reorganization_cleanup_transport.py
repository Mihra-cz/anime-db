"""Actual Chromium HTTP transport: small polling, lazy candidates and safe recovery."""
import json
from pathlib import Path
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ASSETS = Path(__file__).resolve().parents[1] / 'deploy/nexus/service-edge'


@pytest.mark.parametrize('failure', ['rejected', 'disconnect', 'accepted_disconnect'])
def test_cleanup_http_failure_rechecks_authority_then_allows_explicit_decision(tmp_path, failure):
    chromium = shutil.which('chromium')
    if not chromium:
        pytest.skip('Chromium not installed')
    sent, details, polls = [], [], []
    state = {'consumed': False, 'csrf': 'first'}
    source, approved, proposal = 'a'*64, 'b'*64, 'c'*64
    script = '''
window.setInterval=(callback,delay)=>{if(delay===5000)window.refreshCleanup=callback;return 0};
setTimeout(async()=>{try {
 const check=(value,text)=>{if(!value)throw Error(text)};
 const wait=async test=>{for(let i=0;i<100;i++){if(test())return;await new Promise(r=>setTimeout(r,10));}throw Error('condition timed out')};
 await wait(()=>!document.getElementById('cleanup-panel').hidden);
 document.querySelector('#cleanup-list summary').click();
 document.querySelector('[data-cleanup-path="old/0"]').click();
 document.getElementById('cleanup-actor').value='HTTP operator';
 document.getElementById('cleanup-stop').click();
 await wait(()=>!document.getElementById('cleanup-stop').disabled);
 check(!document.getElementById('cleanup-panel').hidden,'rejected decision cannot be corrected');
 await window.refreshCleanup();await window.refreshCleanup();
 check(!document.querySelector('[data-cleanup-path="old/0"]').checked,'HTTP error reset selection');
 check(document.getElementById('cleanup-list').open,'HTTP error collapsed disclosure');
 document.getElementById('cleanup-stop').click();
 await wait(()=>document.getElementById('cleanup-panel').hidden);
 document.body.dataset.test='PASS';
}catch(error){document.body.dataset.test=error.message}},50);
'''
    if failure == 'accepted_disconnect':
        script = script.replace("await wait(()=>!document.getElementById('cleanup-stop').disabled);", "await wait(()=>document.getElementById('cleanup-panel').hidden);")
        script = script.replace("check(!document.getElementById('cleanup-panel').hidden,'rejected decision cannot be corrected');", "check(document.getElementById('cleanup-stop').disabled,'consumed decision remained available');")
    html = (ASSETS/'index.html').read_text().replace('<script defer src="/_service/service.js"></script>',
        '<script>'+script+'</script><script defer src="/_service/service.js"></script>')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith('/_service/workflow'):
                return self.reply(200, html, 'text/html')
            if self.path == '/_service/service.js':
                return self.reply(200,(ASSETS/'service.js').read_text(),'application/javascript')
            if self.path == '/_service/control/cleanup':
                value = dict(waiting=not state['consumed'],source_sha256=source,approved_sha256=approved,
                    proposal_sha256=proposal,candidate_count=279,
                    decision_state='CONSUMED' if state['consumed'] else 'WAITING',phase='cleanup')
                polls.append(json.dumps(value))
                return self.reply(200,value)
            if self.path == '/_service/control/cleanup/proposal?sha='+proposal:
                details.append(1)
                return self.reply(200,dict(waiting=True,source_sha256=source,approved_sha256=approved,
                    proposal=dict(proposal_sha256=proposal,candidates=[dict(path='old/'+str(i),disposition='CLEANUP_REVIEW') for i in range(279)]),csrf=state['csrf']))
            return self.reply(503,{})
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['content-length'])))
            sent.append(body)
            if len(sent)==1:
                state['csrf']='refreshed'
                if failure in {'disconnect','accepted_disconnect'}:
                    state['consumed']=failure=='accepted_disconnect'
                    self.close_connection=True
                    return
                return self.reply(403,dict(error='rejected'))
            state['consumed']=True
            return self.reply(200,dict(accepted=True))
        def reply(self,status,body,content_type='application/json'):
            raw=(body if isinstance(body,str) else json.dumps(body)).encode()
            self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        result=subprocess.run([chromium,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
            '--virtual-time-budget=3500','--dump-dom',f'http://127.0.0.1:{server.server_port}/_service/workflow?source='+source],capture_output=True,text=True,timeout=20)
        assert 'data-test="PASS"' in result.stdout,result.stdout[-2000:]+result.stderr[-1000:]
        tokens = ('first',) if failure=='accepted_disconnect' else ('first','refreshed')
        assert sent==[dict(source_sha256=source,approved_sha256=approved,proposal_sha256=proposal,
            actor='HTTP operator',csrf=token,directories=[],confirm=True,decision='stop') for token in tokens]
        assert len(details)==len(tokens), 'Candidates load once per SHA or once to refresh CSRF after rejection'
        assert len(polls)>=2 and max(map(len,polls))<1024
    finally:
        server.shutdown();server.server_close();thread.join(5)


def test_cleanup_bulk_selection_survives_pages_filters_and_requires_exact_approval(tmp_path):
    chromium = shutil.which('chromium')
    if not chromium:
        pytest.skip('Chromium not installed')
    html = (ASSETS/'index.html').read_text().replace('<script defer src="/_service/service.js"></script>', '')
    setup = '''
const source='a'.repeat(64), approved='b'.repeat(64), proposal='c'.repeat(64);
const candidates=['old','old/child','old/child/leaf',...Array.from({length:120},(_,i)=>'other/'+String(i).padStart(3,'0'))].map(path=>({path,disposition:'REVIEW'}));
const posts=[];let detailLoads=0;
window.setInterval=(fn,delay)=>{if(delay===5000)window.refreshCleanup=fn;return 0};
window.fetch=async (url,options={})=>{
 if(options.method==='POST'){posts.push(JSON.parse(options.body));return new Response(JSON.stringify({accepted:true}));}
 if(url.includes('/cleanup/proposal')){detailLoads++;return new Response(JSON.stringify({waiting:true,source_sha256:source,approved_sha256:approved,csrf:'session',proposal:{proposal_sha256:proposal,candidates}}));}
 if(url==='/_service/control/cleanup')return new Response(JSON.stringify({waiting:true,source_sha256:source,approved_sha256:approved,proposal_sha256:proposal,candidate_count:123,decision_state:'WAITING'}));
 return new Response('{}',{status:503});
};
'''
    check = '''
setTimeout(async()=>{try {
 const el=id=>document.getElementById(id), check=(value,message)=>{if(!value)throw Error(message)};
 const wait=async fn=>{for(let i=0;i<100;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}throw Error('wait expired')};
 const clickPath=path=>{const input=[...document.querySelectorAll('[data-cleanup-path]')].find(n=>n.dataset.cleanupPath===path);check(input,'path absent '+path);input.click()};
 await wait(()=>!el('cleanup-panel').hidden);
 const disclosure=el('cleanup-list');check(disclosure && !disclosure.open,'candidate list must start collapsed');
 check(!el('cleanup-selected').closest('details'),'summary is hidden in disclosure');
 check(!el('cleanup-delete').closest('details'),'confirmation is hidden in disclosure');
 check(disclosure.querySelector('summary').textContent.includes('123'),'collapsed count missing');
 disclosure.querySelector('summary').click();check(disclosure.open,'native disclosure did not open');
 check(document.querySelectorAll('#cleanup-candidates input:checked').length===50,'default all visible');
 check(el('cleanup-selected').textContent.includes('Odstranit: 123 · zachovat: 0'),'default summary');
 check(posts.length===0,'preselection approved itself');
 el('cleanup-next').click();check(document.querySelectorAll('#cleanup-candidates input:checked').length===50,'page default lost');
 el('cleanup-search').value='old';el('cleanup-search').dispatchEvent(new Event('input'));
 clickPath('old/child/leaf');
 check(document.querySelectorAll('#cleanup-candidates input:checked').length===0,'retained leaf did not retain parents');
 await window.refreshCleanup();check(detailLoads===1,'same SHA reloaded');
 check(document.querySelectorAll('#cleanup-candidates input:checked').length===0,'poll reset retained leaf');
 clickPath('old');check(document.querySelectorAll('#cleanup-candidates input:checked').length===3,'parent did not select descendants');
 el('cleanup-none').click();check(el('cleanup-selected').textContent.includes('Odstranit: 0 · zachovat: 123'),'clear only applied to filter');
 el('cleanup-all').click();check(el('cleanup-selected').textContent.includes('Odstranit: 123 · zachovat: 0'),'all only applied to filter');
 clickPath('old/child/leaf');
 el('cleanup-search').value='';el('cleanup-search').dispatchEvent(new Event('input'));el('cleanup-next').click();
 check(document.querySelectorAll('#cleanup-candidates input:checked').length===50,'filter cleared hidden selection');
 check(el('cleanup-selected').textContent.includes('Odstranit: 120 · zachovat: 3'),'final summary');
 disclosure.open=false;await window.refreshCleanup();check(!disclosure.open,'poll opened disclosure');
 check(el('cleanup-selected').textContent.includes('Odstranit: 120 · zachovat: 3'),'collapse reset selection');
 disclosure.querySelector('summary').click();
 el('cleanup-actor').value='Bulk operator';el('cleanup-confirm').checked=true;el('cleanup-sha').value='d'.repeat(64);el('cleanup-delete').click();
 check(posts.length===0,'wrong SHA approved');
 el('cleanup-sha').value=proposal;el('cleanup-confirm').checked=false;el('cleanup-delete').click();check(posts.length===0,'implicit confirmation');
 el('cleanup-confirm').checked=true;el('cleanup-delete').click();await wait(()=>posts.length===1);
 check(posts[0].proposal_sha256===proposal && posts[0].confirm===true,'approval binding');
 check(posts[0].directories.length===120 && posts[0].directories.every(p=>p.startsWith('other/')),'submitted selection differs');
 document.body.dataset.test='PASS';
}catch(error){document.body.dataset.test=error.message}},50);
'''
    page = tmp_path/'bulk.html'
    page.write_text(html.replace('</body>','<script>'+setup+(ASSETS/'service.js').read_text()+check+'</script></body>'))
    result = subprocess.run([chromium,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
        '--virtual-time-budget=3500','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=20)
    assert 'data-test="PASS"' in result.stdout, result.stdout[-2000:]+result.stderr[-1000:]


@pytest.mark.parametrize('restart_state,word', [('failed','selhal'),('timed_out','vypršel'),('available',None)])
def test_service_reports_failed_or_expired_restart_without_claiming_it_is_pending(tmp_path, restart_state, word):
    chromium=shutil.which('chromium')
    if not chromium:pytest.skip('Chromium not installed')
    html=(ASSETS/'index.html').read_text().replace('<script defer src="/_service/service.js"></script>','')
    progress=dict(version=1,run_id='a'*32,source_sha256='b'*64,active=False,updated_at=0,
        completed=14,rename_completed=14,total=14,outcome='NORMAL',app_ready=restart_state=='available',
        app_restart_state=restart_state,stage='normal' if restart_state=='available' else 'app_restart_failed',detail='Result',
        phases=[dict(id=p,state='PASS') for p in ('manifest','folders','rename','sqlite','cleanup','verification')])
    setup='''window.setInterval=()=>0;const progress='''+json.dumps(progress)+''';progress.updated_at=Date.now()/1000;
window.fetch=async url=>url.endsWith('progress.json')?new Response(JSON.stringify(progress),{headers:{date:new Date().toUTCString()}}):new Response(JSON.stringify({waiting:false}),{status:url.endsWith('app-health')?503:200});
'''
    check='''
(async()=>{try {
 const rendered=()=>document.getElementById('service-technical').textContent.includes(progress.stage) &&
   document.getElementById('result').dataset.outcome==='NORMAL';
 await new Promise((resolve,reject)=>{
   const finish=()=>{if(rendered()){clearTimeout(deadline);observer.disconnect();resolve()}};
   const observer=new MutationObserver(finish);
   const deadline=setTimeout(()=>{observer.disconnect();reject(Error('first render timed out'))},2000);
   observer.observe(document.body,{childList:true,subtree:true,characterData:true});finish();
 });
 const text=document.getElementById('message').textContent, word='''+json.dumps(word)+''';
 if(!document.getElementById('workflow-result').hidden)throw Error('unexpected redirect');
 // HTTP health remains unavailable even for the available runtime fixture.
 if(word===null ? !text.includes('Návrat aplikace byl potvrzen hostitelem') || text.includes('zásah správce')
   : !text.includes(word) || !text.includes('zásah správce'))throw Error(text);
 document.body.dataset.test='PASS';
}catch(error){document.body.dataset.test=error.message}})();
'''
    page=tmp_path/'restart.html';page.write_text(html.replace('</body>','<script>'+setup+(ASSETS/'service.js').read_text()+check+'</script></body>'))
    result=subprocess.run([chromium,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
        '--virtual-time-budget=3500','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=20)
    import re
    assert 'data-test="PASS"' in result.stdout, re.findall(r'data-test="[^"]*"',result.stdout)+[result.stderr[-1000:]]
