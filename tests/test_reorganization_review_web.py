"""Review regression: browser sessions, immutable expiry, and host STOP rendering."""
import json
import re
from datetime import datetime,timedelta,timezone

from test_reorganization_approval_web import web,case,production,csrf,prepare
from test_reorganization_web_jobs import prepare_fields,ORIGIN
from test_reorganization_approval import digest


def test_preview_and_approval_tabs_keep_each_others_forms_valid(web):
    case,client,source=web
    path='/reorganization/approval/'+source
    first=client.get(path)
    preview=client.post('/reorganization/preview')
    preview_fields=prepare_fields(preview)
    response=client.post(path+'/prepare',data={'csrf':csrf(first),'actor':'operator',
        'warnings':[w.warning_id for w in case[3].payload.warnings]},headers=ORIGIN)
    assert response.status_code==200,response.text
    response=client.post('/reorganization/prepare',data={**preview_fields,'actor':'operator'},headers=ORIGIN,follow_redirects=False)
    assert response.status_code==303,response.text


def test_opening_second_manifest_does_not_invalidate_first_tab(web):
    from dataclasses import replace
    case,client,source=web
    original=case[3]
    other=replace(original,created_at='2026-10-19T12:00:01Z')
    runtime=replace(case[4],snapshot_at='2026-10-19T12:00:02Z')
    next_source=case[0].publish_proposal(inbox=case[5],database=case[2],library_root=case[1].root,
        share='//synology.test/Anime',share_root='/',manifest=other,runtime=runtime)
    page=client.get('/reorganization/approval/'+source)
    client.get('/reorganization/approval/'+next_source)
    response=client.post('/reorganization/approval/'+source+'/prepare',data={'csrf':csrf(page),'actor':'operator',
        'warnings':[w.warning_id for w in original.payload.warnings]},headers=ORIGIN)
    assert response.status_code==200,response.text


def test_approval_reports_coordinator_stop_instead_of_waiting(web):
    case,client,source=web
    path,page,final=prepare(web)
    page=client.post(path+'/approve',data={'csrf':csrf(page),'sha256':digest(final),'actor':'operator','confirm':'yes'},headers=ORIGIN)
    client.post(path+'/start',data={'csrf':csrf(page),'sha256':digest(final),'actor':'operator','confirm':'yes'},headers=ORIGIN,follow_redirects=False)
    case[0].write_record(case[5],source+'.start-status.json',json.dumps(dict(version=1,kind='start',request_id=source,
        state='STOP',phase='pre_execution_failed',detail='Platnost manifestu vypršela. E7 nezačalo.',source_sha256=source)))
    page=client.get(path)
    assert 'E7 nezačalo' in page.text and 'Požadavek čeká na ověření hostitelem' not in page.text


def test_approval_shows_exact_expiry_and_server_remaining_seconds(web):
    case,client,source=web
    page=client.get('/reorganization/approval/'+source)
    bundle=case[0].read_proposal(case[5],source)
    expected=(datetime.fromisoformat(bundle['published_at'])+timedelta(seconds=900)).isoformat().replace('+00:00','Z')
    assert f'data-expires-at="{expected}"' in page.text
    assert re.search(r'data-remaining-seconds="[0-9]+"',page.text)
    assert 'Zbývající platnost' in page.text


def test_approval_rechecks_expiry_after_slow_preflight(web,monkeypatch):
    from app import reorganization_approval_web as module
    case,client,source=web
    path,page,final=prepare(web)
    def expires(*args,**kwargs):
        file=case[5]/(source+'.proposal.json')
        bundle=json.loads(file.read_text());bundle['published_at']='2000-01-01T00:00:00Z';file.write_text(json.dumps(bundle))
        return dict(outcome='READY_FOR_EXECUTION',diagnostics=[])
    monkeypatch.setattr(module,'run_check',expires)
    response=client.post(path+'/approve',data={'csrf':csrf(page),'sha256':digest(final),'actor':'operator','confirm':'yes'},headers=ORIGIN)
    assert response.status_code==409
    assert not (case[6]/(source+'.approval.json')).exists()


def test_browser_countdown_disables_expired_approval_form(web,tmp_path):
    import shutil,subprocess
    from pathlib import Path
    import pytest
    chromium=shutil.which('chromium')
    if not chromium:pytest.skip('Chromium unavailable')
    case,client,source=web
    page=client.get('/reorganization/approval/'+source).text
    page=re.sub(r'data-remaining-seconds="[0-9]+"','data-remaining-seconds="1"',page)
    page=re.sub(r'<script src="[^"]*reorganization_approval.js" defer></script>','',page)
    page=re.sub(r'<script src="[^"]*reorganization_presentation.js" defer></script>','',page)
    script=(Path(__file__).parents[1]/'app/static/reorganization_presentation.js').read_text()+'\n'+(Path(__file__).parents[1]/'app/static/reorganization_approval.js').read_text()
    check='''setTimeout(()=>{document.body.dataset.test=document.getElementById('manifest-validity').dataset.expired==='yes'&&document.getElementById('manifest-prepare-submit').disabled?'PASS':'FAIL'},1200);'''
    file=tmp_path/'expiry.html';file.write_text(page.replace('</body>','<script>'+script+check+'</script></body>'))
    result=subprocess.run([chromium,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
        '--virtual-time-budget=1600','--dump-dom',file.as_uri()],capture_output=True,text=True,timeout=20)
    assert 'data-test="PASS"' in result.stdout,result.stdout[-1500:]+result.stderr[-1000:]


def test_public_stop_report_keeps_private_error_and_evidence_paths_private(web):
    case,client,source=web
    path,page,final=prepare(web)
    total=len(final.payload.actions)
    record=dict(source_sha256=source,approved_sha256=digest(final),manifest_id=final.manifest_id,
        state='STOP',started=False,completed=0,total=total,remaining=total,actor='operator',phase='CLEANUP',
        finished_at=case[0].now(),removed_directories=None,error='password=PRIVATE_SECRET inside private system',
        journal_path='/PRIVATE_EVIDENCE_ROOT/journal',guard_path='/PRIVATE_EVIDENCE_ROOT/guard',
        cleanup_evidence_path='/PRIVATE_EVIDENCE_ROOT/cleanup')
    case[0].write_record(case[5],source+'.result.json',json.dumps(record))
    response=client.get(path)
    assert 'PRIVATE_SECRET' not in response.text and 'PRIVATE_EVIDENCE_ROOT' not in response.text
    assert 'STOP' in response.text
    assert case[0].read_execution_result(case[5],source,final)==record
