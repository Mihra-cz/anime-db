"""Presentation regressions over synthetic receipts; never execute E7/E7H."""
import json
import re

import pytest

from test_reorganization_approval_web import web, case, production, csrf
from test_reorganization_approval import digest, snapshot


def test_messages_explain_real_meaning_and_keep_unknown_evidence():
    from app.reorganization_presentation import describe
    missing = describe('subtitle_present_video_missing', 'warning')
    assert missing['title'] == 'Titulky bez odpovídajícího videa'
    assert 'potvrzen' in missing['reason'] and 'přesun' in missing['impact']
    assert 'cílové' in missing['action']
    assert describe('windows_path_budget', 'warning')['title'] == 'Dlouhá cesta pro Windows'
    # Preflight keeps an over-budget path NOT_READY even when acknowledged: shortening is the only remedy.
    budget = describe('windows_path_budget', 'warning')
    assert 'zkrátit' in budget['action'] and 'nebo upozornění' not in budget['action']
    assert 'Potvrzení upozornění na tom nic nemění' in budget['impact']
    # Only unresolved associations and saved archive decisions have the Archive Review form.
    assert 'Vyřešit' in describe('duplicate_side_asset_association_unresolved')['action']
    for code in ('archive_review_source_changed', 'archive_review_context_changed',
                 'archive_review_duplicate_changed', 'archive_review_target_invalid'):
        assert 'Změnit / zrušit rozhodnutí' in describe(code)['action']
    for code in ('known_side_asset_not_in_inventory', 'duplicate_side_asset_evidence_mismatch',
                 'carried_side_asset_evidence_mismatch', 'side_asset_evidence_conflicts_with_subs_lane'):
        action = describe(code)['action']
        assert 'Vyřešit' not in action and 'tlačítko' not in action and 'Nejde o rozhodnutí o přiřazení archivu' in action
    assert describe('manual_duplicate_suspected', 'review')['title'] == 'Podezření na duplicitu čeká na kontrolu'
    assert 'potvrzenou duplicitu' in describe('manual_duplicate_suspected', 'review')['reason']
    assert describe('target_collision:exact_key', 'error')['title'] == 'Cílové cesty spolu kolidují'
    assert describe('approved_root_txt_keep', 'info')['action'] == 'Není potřeba další rozhodnutí.'
    unknown = describe('future_diagnostic:<script>', 'review')
    assert unknown['title'] == 'Podklad vyžaduje kontrolu'
    assert unknown['code'] == 'future_diagnostic:<script>'
    assert unknown['severity'] == 'review'


@pytest.mark.parametrize('restart', [None, 'failed', 'timed_out', 'available'])
def test_result_separates_verified_completion_and_app_return_without_writes(web, restart):
    fixture, client, source = web
    api, owner, database, manifest, _, inbox, outbox, *_ = fixture
    before = snapshot(database, owner.root)
    # Published evidence fixture only: no engine, startup, filesystem moves or scanner.
    record=dict(source_sha256=source,approved_sha256=digest(manifest),manifest_id=manifest.manifest_id,
        state='NORMAL',started=True,completed=len(manifest.payload.actions),total=len(manifest.payload.actions),remaining=0,
        actor='Mirek Žák',phase='NORMAL',finished_at=api.now(),removed_directories=['empty/'+str(i) for i in range(11)],
        error=None,journal_path='/PRIVATE/journal',guard_path='/PRIVATE/guard',cleanup_evidence_path='/PRIVATE/cleanup')
    api.write_record(inbox, source+'.result.json',api._json(record))
    if restart is not None:
        api.write_record(outbox,source+'.start-request.json',api._json(dict(version=1,kind='start',request_id=source,
            source_sha256=source,approved_sha256=digest(manifest),manifest_id=manifest.manifest_id,actor='Mirek Žák',requested_at=api.now())))
        api.write_record(inbox,source+'.start-status.json',api._json(dict(version=1,kind='start',request_id=source,
            source_sha256=source,state='NORMAL',phase='normal' if restart=='available' else 'app_restart_failed',
            detail='Host evidence',app_restart_state=restart)))
    page=client.get('/reorganization/approval/'+source)
    assert page.status_code == 200
    assert 'Reorganizace dokončena' in page.text
    assert 'Odstraněné prázdné adresáře: 11' in page.text
    assert 'Změny cest v databázi ověřeny' in page.text
    assert 'Mirek Žák' in page.text
    assert ('Návrat aplikace byl potvrzen' in page.text) is (restart=='available')
    assert 'href="/"' in page.text and 'Zpět do katalogu' in page.text
    assert '/PRIVATE/' not in page.text
    assert 'Technické podrobnosti' in page.text
    # A completed run shows the summary only, never the pre-approval guidance.
    assert 'data-view-state="normal"' in page.text and 'Soubory byly zpracovány' in page.text
    for stale in ('Nejdříve potvrďte upozornění', 'Schválení návrhu reorganizaci nespouští',
                  'Opravit autoritativní rozhodnutí', 'Úklid prázdných adresářů po přesunech'):
        assert stale not in page.text
    assert snapshot(database, owner.root) == before


def test_guidance_follows_draft_and_stop_states_without_writes(web):
    fixture, client, source = web
    api, owner, database, manifest, _, inbox, *_ = fixture
    before = snapshot(database, owner.root)
    path = '/reorganization/approval/' + source
    draft = client.get(path).text
    assert 'data-view-state="draft"' in draft and 'Nejdříve potvrďte upozornění' in draft
    assert 'Návrh reorganizace ke schválení' in draft
    total = len(manifest.payload.actions)
    api.write_record(inbox, source + '.result.json', api._json(dict(source_sha256=source, approved_sha256=digest(manifest),
        manifest_id=manifest.manifest_id, state='STOP', started=True, completed=1, total=total, remaining=total - 1,
        actor='Mirek Žák', phase='CLEANUP', finished_at=api.now(), removed_directories=None, error='private detail',
        journal_path='/PRIVATE/journal', guard_path='/PRIVATE/guard', cleanup_evidence_path='/PRIVATE/cleanup')))
    stop = client.get(path).text
    assert 'data-view-state="stop"' in stop and 'Reorganizace zastavena' in stop
    assert 'Reorganizace byla zastavena a vyžaduje zásah správce' in stop and 'nic se automaticky neopakuje' in stop
    assert 'Nejdříve potvrďte upozornění' not in stop and 'Reorganizace dokončena' not in stop
    assert snapshot(database, owner.root) == before


def test_named_review_approval_and_start_preserve_actor_and_canonical_bytes(web, monkeypatch):
    fixture, client, source=web
    api,owner,database,manifest,runtime,inbox,outbox,ledger,lock=fixture
    before=snapshot(database,owner.root)
    path='/reorganization/approval/'+source
    actor='Mirek Žák'
    page=client.get(path)
    page=client.post(path+'/prepare',data=dict(csrf=csrf(page),actor=actor,
        warnings=[w.warning_id for w in manifest.payload.warnings]),headers={'origin':'http://testserver'})
    assert page.status_code==200
    final=api.read_review(outbox,source)
    assert api.read_review_actor(outbox,source,final)==actor
    assert all(ack.actor==actor for ack in final.acknowledgements)
    canonical=client.get(path+'/manifest').content
    page=client.post(path+'/approve',data=dict(csrf=csrf(page),actor=actor,sha256=digest(final),confirm='yes'),headers={'origin':'http://testserver'})
    assert page.status_code==200 and actor in page.text
    assert 'data-view-state="approved"' in page.text and 'Návrh je schválený, reorganizace ale ještě neběží' in page.text
    assert api.read_approval(outbox,source)['actor']==actor
    start=client.post(path+'/start',data=dict(csrf=csrf(page),actor=actor,sha256=digest(final),confirm='yes'),headers={'origin':'http://testserver'},follow_redirects=False)
    assert start.status_code==303
    running=client.get(path).text
    assert 'data-view-state="running"' in running and 'Spuštění bylo předáno hostiteli' in running
    assert 'Nejdříve potvrďte upozornění' not in running
    assert json.loads(api.read_record(outbox,source+'.start-request.json'))['actor']==actor
    assert client.get(path+'/manifest').content==canonical
    # Exercise the real transport/publication adapters with an engine stub only.
    # No physical E7/E7H actions, DB projection or startup are permitted here.
    from dataclasses import replace
    from app import reorganization_execution as execution, target_execution_cleanup as cleanup
    from app.reorganization_cleanup_web import WebCleanupDecision
    proposal=cleanup.CleanupProposal(final.manifest_id,digest(final),'journal',(1,2),'a'*64,
        str(owner.root),(1,3),'mount',1,'b'*64,'c'*64,'d'*64,(),(),
        (cleanup.CleanupCandidate('empty',(1,4),'UNRESOLVED_EMPTY'),),'')
    proposal=replace(proposal,proposal_sha256=cleanup._seal(proposal,'proposal_sha256'))
    api.write_record(ledger,source+'.cleanup-decision.json',api._json(dict(source_sha256=source,
        approved_sha256=digest(final),proposal_sha256=proposal.proposal_sha256,actor=actor,directories=['empty'],confirm=True)))
    decision=WebCleanupDecision(database=database,ledger=ledger,source_sha256=source,approved_sha256=digest(final))
    def engine_stub(**kwargs):
        approved=kwargs['cleanup_decision'](proposal)
        assert approved.actor==actor and approved.directories==('empty',)
        kwargs['progress'](dict(phase='NORMAL',completed=len(final.payload.actions),action_index=0))
        return dict(removed_directories=['empty'])
    monkeypatch.setattr(execution,'execute_approved_reorganization',engine_stub)
    result=execution.execute_from_approval(inbox=inbox,outbox=outbox,ledger=ledger,source_sha256=source,
        approved_sha256=digest(final),actor=actor,database=database,library_root=owner.root,
        share='//synology.test/Anime',share_root='/',runtime=runtime,runtime_lock=lock,writers_stopped=True,cleanup_decision=decision)
    assert result['actor']==actor and api.read_execution_result(inbox,source,final)['actor']==actor
    assert json.loads(api.read_record(ledger,source+'.cleanup-decision.json'))['actor']==actor
    assert actor in client.get(path).text
    assert snapshot(database,owner.root)==before


def test_chromium_messages_show_czech_guidance_and_collapsed_exact_codes(tmp_path):
    import shutil, subprocess
    from pathlib import Path
    from app.reorganization_presentation import catalog
    chromium=shutil.which('chromium')
    if not chromium:pytest.skip('Chromium unavailable')
    helper=Path('app/static/reorganization_presentation.js').read_text()
    setup='''const P=window.ReorganizationPresentation;
const codes=[['subtitle_present_video_missing','warning'],['manual_duplicate_suspected','review'],['target_collision:file_directory','error'],['archive_source_preserved_contents_not_classified','info']];
for(const [code,level] of codes){const section=document.createElement('section');section.append(P.message(code,level),P.technical(code));document.body.append(section);}
const raw='unrecognized:<img src=x onerror=alert(1)>';document.body.append(P.message(raw,'review'),P.technical(raw));
const check=(test,message)=>{if(!test)throw Error(message)};
try {
check(document.body.innerText.includes('Titulky bez odpovídajícího videa'),'Czech title absent');
check(document.body.innerText.includes('Upozornění')&&document.body.innerText.includes('Informace')&&document.body.innerText.includes('Blokující problém'),'severities lost');
check(!document.body.innerText.includes('subtitle_present_video_missing'),'code primary');
check([...document.querySelectorAll('details')].every(n=>!n.open),'technical details expanded');
check(document.body.textContent.includes(raw)&&!document.querySelector('img'),'raw code evidence lost or unsafe');
document.body.dataset.test='PASS';
}catch(error){document.body.dataset.test=error.message}
'''
    page=tmp_path/'messages.html'
    page.write_text('<!doctype html><html lang="cs"><meta charset="utf-8"><body><script type="application/json" id="reorganization-messages">'+json.dumps(catalog())+'</script><script>'+helper+setup+'</script></body></html>')
    result=subprocess.run([chromium,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--virtual-time-budget=500','--dump-dom',page.as_uri()],text=True,capture_output=True,timeout=20)
    assert 'data-test="PASS"' in result.stdout,result.stdout[-1500:]+result.stderr[-500:]


def test_preflight_diagnostics_are_explained_without_approving_or_changing_evidence(web, monkeypatch):
    from app import reorganization_approval_web as module
    fixture, client, source=web
    api,owner,database,manifest,_,_,outbox,*_=fixture
    before=snapshot(database,owner.root)
    monkeypatch.setattr(module,'run_check',lambda *a,**k:dict(outcome='NOT_READY',diagnostics=['target_collision:fold_key']))
    path='/reorganization/approval/'+source
    page=client.get(path)
    response=client.post(path+'/prepare',data=dict(csrf=csrf(page),actor='Mirek Žák',warnings=[w.warning_id for w in manifest.payload.warnings]),headers={'origin':'http://testserver'})
    assert response.status_code==409
    assert 'Cílové cesty spolu kolidují' in response.text and 'Technické podrobnosti' in response.text
    assert 'target_collision:fold_key' in response.text
    assert not (outbox/(source+'.approval.json')).exists()
    assert snapshot(database,owner.root)==before


@pytest.mark.parametrize('outcome', ['UNVERIFIED','STOP','NORMAL'])
def test_chromium_service_translates_states_and_confirmed_phase_without_claiming_restart(tmp_path, outcome):
    import shutil, subprocess
    from pathlib import Path
    chromium=shutil.which('chromium')
    if not chromium:pytest.skip('Chromium unavailable')
    assets=Path('deploy/nexus/service-edge')
    phases=['manifest','folders','rename','sqlite','cleanup','verification']
    states=['PASS']*6 if outcome=='NORMAL' else ['PASS','PASS','STOP' if outcome=='STOP' else 'WORK','WAIT','WAIT','WAIT']
    progress=dict(version=1,run_id='a'*32,source_sha256='b'*64,active=outcome=='UNVERIFIED',updated_at=0,
        completed=14 if outcome=='NORMAL' else 2,rename_completed=14 if outcome=='NORMAL' else 2,total=14,
        outcome=outcome,app_ready=False,current_phase='verification' if outcome=='NORMAL' else 'rename',
        last_confirmed=dict(phase='verification' if outcome=='NORMAL' else 'manifest',state='PASS',completed=2),
        phases=[dict(id=p,state=s) for p,s in zip(phases,states)])
    setup='''window.setInterval=()=>0;const progress='''+json.dumps(progress)+''';progress.updated_at=Date.now()/1000;
window.fetch=async url=>url.endsWith('progress.json')?new Response(JSON.stringify(progress),{headers:{date:new Date().toUTCString()}}):new Response(JSON.stringify({waiting:false}),{status:url.endsWith('app-health')?503:200});
'''
    checks='''(async()=>{try {
for(let i=0;document.getElementById('result').dataset.outcome!==progress.outcome;i++){
 if(i===200)throw Error('render timed out');await new Promise(r=>setTimeout(r,10));}
const check=(test,message)=>{if(!test)throw Error(message)};
const labels={WAIT:'Čeká se',WORK:'Právě probíhá',PASS:'Dokončeno',STOP:'Zastaveno, vyžaduje zásah'};
for(const row of progress.phases)check(document.querySelector('[data-phase="'+row.id+'"]').cells[1].textContent===labels[row.state],'state label differs');
check(document.getElementById('confirmed').textContent.includes((progress.outcome==='NORMAL'?'Závěrečné ověření':'Příprava návrhu')+'. Dokončených operací:'),'confirmed phase phrased as ongoing');
check([...document.querySelectorAll('p')].filter(p=>p.textContent.includes('Předvýběr není schválení')).length===1,'preselection notice duplicated');
check(document.getElementById('message').textContent.includes(progress.outcome==='NORMAL'?'Čeká se na hostitelem ověřený restart':progress.outcome==='STOP'?'zastavena':'Hostitel zpracovává'),'outcome/restart label differs');
check(document.getElementById('service-technical').textContent.includes(progress.outcome),'technical state lost');
check(!document.getElementById('service-technical').parentElement.open,'technical details expanded');
document.body.dataset.test='PASS';
}catch(error){document.body.dataset.test=error.message}})();'''
    page=tmp_path/'service.html'
    html=(assets/'index.html').read_text().replace('<script defer src="/_service/service.js"></script>','')
    page.write_text(html.replace('</body>','<script>'+setup+(assets/'service.js').read_text()+checks+'</script></body>'))
    result=subprocess.run([chromium,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--virtual-time-budget=3500','--dump-dom',page.as_uri()],text=True,capture_output=True,timeout=20)
    assert 'data-test="PASS"' in result.stdout,re.findall(r'data-test="[^"]*"',result.stdout)+[result.stderr[-500:]]


def test_preview_validation_suffix_has_czech_explanation_and_unchanged_conflict(web, monkeypatch):
    from app import reorganization_web as module
    from app.reorganization_archive_review import ArchiveReviewConflict
    fixture,client,_=web
    def conflict(*args,**kwargs):raise ArchiveReviewConflict('Rozhodnutí nemá platný cíl bez kolize: target_collision:file_directory')
    monkeypatch.setattr(module,'run_preview',conflict)
    before=snapshot(fixture[2],fixture[1].root)
    response=client.post('/reorganization/preview')
    assert response.status_code==409
    assert 'Cílové cesty spolu kolidují' in response.text
    assert re.search(r'<div class="notice error" role="alert">([^<]*)</div>',response.text)[1]=='Rozhodnutí nemá platný cíl bez kolize'
    assert '<pre class="reorganization-path">Rozhodnutí nemá platný cíl bez kolize: target_collision:file_directory</pre>' in response.text
    assert snapshot(fixture[2],fixture[1].root)==before
