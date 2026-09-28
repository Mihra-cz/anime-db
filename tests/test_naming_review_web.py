"""Explicit naming HTTP decisions preserve every other domain in fresh sessions."""
import html
import json
import re
from urllib.parse import urlencode, urlsplit, parse_qsl

import asyncio
import inspect
import httpx
from fastapi import HTTPException
from markupsafe import escape
from starlette.requests import Request
from starlette.routing import Match
import pytest
from sqlalchemy import event, select

from app.config import Settings
from app.database import Base
from app.main import create_app
from app.models import CatalogCollection, CatalogTitle, PhysicalNamingChoice, Video, ManualSplitRuleVideo, VideoVariantGroup
from app.physical_naming_service import load_physical_naming_context, resolve_physical_name
from test_physical_naming import collection, title, attach_metadata, domain_snapshot, NOW
from test_naming_review import review_api


class EndpointClient:
    """Exercise routes with real Request/form parsing, like existing web tests."""
    def __init__(self, app): self.app=app
    def get(self,path,**kwargs): return self.request('GET',path,**kwargs)
    def post(self,path,**kwargs): return self.request('POST',path,**kwargs)
    def request(self,method,path,data=None,headers=None,follow_redirects=False):
        url=urlsplit(path)
        body=urlencode(data or {}).encode()
        request_headers={'content-type':'application/x-www-form-urlencoded',**(headers or {})}
        scope={'type':'http','app':self.app,'method':method,'path':url.path,'root_path':'','scheme':'http','query_string':url.query.encode(),'headers':[(k.lower().encode(),v.encode()) for k,v in request_headers.items()],'server':('testserver',80),'client':('testclient',1)}
        async def receive(): return {'type':'http.request','body':body,'more_body':False}
        request=Request(scope,receive)
        for route in self.app.routes:
            match, child=route.matches(scope)
            if match is not Match.FULL: continue
            args={**child.get('path_params',{})}
            params=inspect.signature(route.endpoint).parameters
            if 'request' in params: args['request']=request
            for k,v in parse_qsl(url.query):
                if k in params: args[k]=int(v) if k in {'collection_id','title_id','page'} else v
            try:
                response=route.endpoint(**args)
                if inspect.isawaitable(response): response=asyncio.run(response)
            except HTTPException as exc:
                return httpx.Response(exc.status_code,text=str(exc.detail))
            return httpx.Response(response.status_code,content=response.body,headers=dict(response.headers))
        return httpx.Response(404,text='Not Found')


@pytest.fixture
def web(tmp_path):
    app=create_app(Settings(anime_path=tmp_path/'media',database_url=f"sqlite:///{tmp_path/'review.db'}",metadata_artwork_directory=tmp_path/'artwork',metadata_download_artwork=False))
    Base.metadata.create_all(app.state.sessions.kw['bind'])
    with app.state.sessions() as session:
        root=collection(session,'Current Root')
        main=title(session,root,'Current Prefix',metadata=False)
        attach_metadata(main,'1','Long Romaji: full title for Review')
        group=VideoVariantGroup(catalog_title=main,manual_label='BD',verified_at=NOW)
        video=Video(catalog_title=main,catalog_collection=root,filename='Show - 01.MKV',relative_path='Show/01.MKV',root_folder='Show',size=1,mtime_ns=1,season_episode_number=1,absolute_episode_number=99,content_type_manual='episode',media_part_number=1,video_variant_group=group)
        session.add(video);session.flush()
        session.add(ManualSplitRuleVideo(catalog_title=main,video=video))
        session.commit()
        ids=root.id,main.id
    return app,EndpointClient(app),ids


def unit(app,scope,id):
    api,service=review_api()
    with app.state.sessions() as session:
        return api.build_naming_review(service.load_naming_review_context(session)).units[scope,id]


def post(app,client,scope,id,kind,**extra):
    row=unit(app,scope,id)
    key=kind if kind in ('custom','reconfirm','reset') else next(c.key for c in row.candidates if c.kind==kind)
    data=dict(fingerprint=row.fingerprint,candidate_key=key,return_to='/naming-review?status=all&q=Current',**extra)
    action=kind if kind in ('reset','reconfirm') else 'save'
    return client.post(f'/naming-review/{scope}/{id}/{action}',data=data,follow_redirects=False)


def snapshot(app):
    with app.state.sessions() as session:return domain_snapshot(session)


def test_new_route_navigation_default_queue_and_filters(web):
    app,client,(root,main)=web
    page=client.get('/naming-review')
    assert page.status_code==200, 'Naming Review UI route is missing'
    assert 'Pojmenování' in page.text and 'K vyřízení' in page.text
    assert 'href="/naming-review"' in client.get('/').text
    for label in ('Root názvy','Názvy částí','Potvrzené','Vše'):assert label in page.text
    assert f'data-naming-owner="collection:{root}"' in page.text
    assert f'data-naming-owner="title:{main}"' in page.text
    assert 'Automatický výchozí' in page.text
    assert 'Filesystem' in page.text and 'UTF-8' in page.text
    assert client.get('/naming-review?q=no-match').text.count('data-naming-owner=')==0
    rootpage=client.get('/naming-review?status=roots')
    assert f'data-naming-owner="collection:{root}"' in rootpage.text
    assert f'data-naming-owner="title:{main}"' not in rootpage.text


@pytest.mark.parametrize('scope',['collection','title'])
@pytest.mark.parametrize('kind,text',[('romaji','Long Romaji: full title for Review'),('english','English'),('synonym','Short'),('current',None),('custom','Cafe\u0301: 日本語?')])
def test_each_save_flow_isolated_raw_snapshot_and_prg(web,scope,kind,text):
    app,client,(root,main)=web
    id=root if scope=='collection' else main
    before=snapshot(app)
    response=post(app,client,scope,id,kind,custom_text=text or '' if kind=='custom' else '')
    assert response.status_code==303
    assert response.headers['location']=='/naming-review?status=all&q=Current'
    with app.state.sessions() as session:
        choice=session.scalar(select(PhysicalNamingChoice))
        assert choice.choice_kind==kind
        expected=text or ('Current Root' if scope=='collection' else 'Current Prefix')
        assert choice.physical_text==expected
    assert snapshot(app)==before
    row=unit(app,scope,id)
    assert row.resolution.authority=='human_choice' and not row.actionable
    page=client.get('/naming-review?status=confirmed')
    assert 'Člověkem potvrzený' in page.text and 'Používat automatický/default název' in page.text


def test_reset_returns_to_derived_without_domain_writes(web):
    app,client,(root,main)=web
    assert post(app,client,'title',main,'romaji').status_code==303
    before=snapshot(app)
    assert post(app,client,'title',main,'reset').status_code==303
    with app.state.sessions() as session:assert session.scalar(select(PhysicalNamingChoice)) is None
    assert unit(app,'title',main).resolution.authority=='derived_default'
    assert snapshot(app)==before


def test_metadata_relink_mismatch_reconfirm_preserves_text(web):
    app,client,(_,main)=web
    assert post(app,client,'title',main,'romaji').status_code==303
    with app.state.sessions() as session:
        owner=session.get(CatalogTitle,main)
        owner.external_links[0].external_id='2'
        owner.metadata_record.metadata_external_id='2'
        session.commit()
    before=snapshot(app)
    page=client.get('/naming-review')
    assert 'Metadata kontext se změnil od posledního potvrzení názvu.' in page.text
    assert 'Potvrdit tento název znovu' in page.text
    assert post(app,client,'title',main,'reconfirm').status_code==303
    assert unit(app,'title',main).resolution.basis_matches and not unit(app,'title',main).actionable
    assert snapshot(app)==before


@pytest.mark.parametrize('action',['save','reset','reconfirm'])
def test_stale_form_is_rejected_without_naming_or_domain_write(web,action):
    app,client,(_,main)=web
    assert post(app,client,'title',main,'romaji').status_code==303
    old=unit(app,'title',main)
    candidate=next(c for c in old.candidates if c.kind=='synonym')
    with app.state.sessions() as session:
        session.get(CatalogTitle,main).metadata_record.synonyms_json='["Different"]'
        session.commit()
    before=snapshot(app)
    response=client.post(f'/naming-review/title/{main}/{action}',data={'fingerprint':old.fingerprint,'candidate_key':candidate.key},follow_redirects=False)
    assert response.status_code==409
    assert 'Kontext pojmenování se změnil' in response.text
    with app.state.sessions() as session:assert session.scalar(select(PhysicalNamingChoice)).choice_kind=='romaji'
    assert snapshot(app)==before


def test_synonym_order_change_cannot_change_selection_meaning(web):
    app,client,(_,main)=web
    with app.state.sessions() as session:
        session.get(CatalogTitle,main).metadata_record.synonyms_json='["First", "Second"]';session.commit()
    old=unit(app,'title',main)
    key=next(c.key for c in old.candidates if c.raw_text=='First')
    with app.state.sessions() as session:
        session.get(CatalogTitle,main).metadata_record.synonyms_json='["Second", "First"]';session.commit()
    response=client.post(f'/naming-review/title/{main}/save',data={'fingerprint':old.fingerprint,'candidate_key':key},follow_redirects=False)
    assert response.status_code==303
    with app.state.sessions() as session:assert session.scalar(select(PhysicalNamingChoice)).physical_text=='First'


@pytest.mark.parametrize('text',['','a\tb','あ'*86,'a'*243])
def test_custom_invalid_or_known_filename_overflow_has_no_partial_write(web,text):
    app,client,(_,main)=web
    before=snapshot(app)
    response=post(app,client,'title',main,'custom',custom_text=text)
    assert response.status_code==400
    with app.state.sessions() as session:assert session.scalar(select(PhysicalNamingChoice)) is None
    assert snapshot(app)==before
    if len(text)>200 or text.startswith('あ'):
        assert 'Název je příliš dlouhý. Urči kratší název.' in response.text
        assert '255' in response.text and 'Překročeno o' in response.text


@pytest.mark.parametrize('stale',[False,True])
def test_rejected_decision_keeps_callers_return_to_for_the_retry(web,stale):
    app,client,(_,main)=web
    target='/naming-review?status=titles&q=Current'
    row=unit(app,'title',main)
    response=client.post(f'/naming-review/title/{main}/save',data={
        'fingerprint':'stale' if stale else row.fingerprint,'candidate_key':'custom','custom_text':'a\tb','return_to':target,
    })
    assert response.status_code==(409 if stale else 400)
    naming_forms=re.findall(r'<form method="post" action="/naming-review/.*?</form>',response.text,re.S)
    carried={value for form in naming_forms for value in re.findall(r'name="return_to" value="([^"]*)"',form)}
    assert carried=={html.escape(target)}
    retry=client.post(f'/naming-review/title/{main}/save',data={
        'fingerprint':row.fingerprint,'candidate_key':'custom','custom_text':'Fixed','return_to':html.unescape(carried.pop()),
    })
    assert (retry.status_code,retry.headers['location'])==(303,target)


def test_client_cannot_forge_candidate_text_or_kind_or_cross_origin(web):
    app,client,(_,main)=web
    row=unit(app,'title',main)
    data={'fingerprint':row.fingerprint,'candidate_key':next(c.key for c in row.candidates if c.kind=='synonym'),'kind':'custom','text':'FORGED'}
    assert client.post(f'/naming-review/title/{main}/save',data=data).status_code==400
    data.pop('kind');data.pop('text')
    assert client.post(f'/naming-review/title/{main}/save',data=data,headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/naming-review/title/999999/save',data=data).status_code==404
    assert client.post('/naming-review/wrong/1/save',data=data).status_code==404


def test_malformed_origin_is_rejected_as_forbidden_not_server_error(web):
    app,client,(_,main)=web
    row=unit(app,'title',main)
    response=client.post(f'/naming-review/title/{main}/save',headers={'Origin':'https://['},data={
        'fingerprint':row.fingerprint,'candidate_key':'custom','custom_text':'Safe',
    })
    assert response.status_code==403


def test_untrusted_unicode_fingerprint_is_rejected_without_server_error(web):
    app,client,(_,main)=web
    response=client.post(f'/naming-review/title/{main}/save',data={
        'fingerprint':'あ','candidate_key':'custom','custom_text':'Safe',
    })
    assert response.status_code==409
    with app.state.sessions() as session:
        assert session.scalar(select(PhysicalNamingChoice)) is None


@pytest.mark.parametrize('season',[2,4])
def test_parent_prefix_choice_never_sets_part_and_remains_snapshot(web,season):
    app,client,(root,main)=web
    with app.state.sessions() as session:
        owner=session.get(CatalogCollection,root)
        p1=title(session,owner,'P1',season=season,part=1,kind='part')
        p2=title(session,owner,'P2',season=season,part=2,kind='part')
        child=title(session,owner,'NCOP',season=season,part=None,kind='bonus',metadata=False)
        session.commit();ids=p1.id,p2.id,child.id
    p1,p2,child=ids
    assert post(app,client,'title',p1,'custom',custom_text='Short P1').status_code==303
    before=snapshot(app)
    assert post(app,client,'title',child,'parent_prefix').status_code==303
    with app.state.sessions() as session:
        saved=session.get(CatalogTitle,child)
        assert (saved.effective_season_number,saved.effective_part_number)==(season,None)
        choice=session.scalar(select(PhysicalNamingChoice).where(PhysicalNamingChoice.catalog_title_id==child))
        assert choice.physical_text=='Short P1' and choice.choice_kind=='parent_prefix'
        assert json.loads(choice.basis_snapshot_json)['source_title_id']==p1
    assert snapshot(app)==before
    assert post(app,client,'title',p1,'custom',custom_text='New parent name').status_code==303
    assert unit(app,'title',child).resolution.effective_text=='Short P1'


def test_bananya_root_confirmation_closes_naming_without_creating_anchor(web):
    app,client,(root,main)=web
    with app.state.sessions() as session:
        owner=session.get(CatalogCollection,root);owner.local_title='Bananya (J23)'
        t=session.get(CatalogTitle,main);t.season_number_manual=2;t.season_number=2
        t.external_links[0].external_id='2';t.metadata_record.metadata_external_id='2';t.metadata_record.title_romaji='Bananya: Fushigi na Nakamatachi';session.commit()
    assert unit(app,'collection',root).resolution.authority=='ambiguous'
    before=snapshot(app)
    assert post(app,client,'collection',root,'current').status_code==303
    row=unit(app,'collection',root)
    assert row.resolution.effective_text=='Bananya' and not row.actionable
    assert row.resolution.metadata_source_title_id is None
    assert snapshot(app)==before


def test_badges_link_to_relevant_queue_and_all_gets_are_read_only(web):
    app,client,(root,main)=web
    engine=app.state.sessions.kw['bind'];writes=[]
    before=snapshot(app)
    def record(c,cursor,statement,p,context,many):
        if statement.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')):writes.append(statement)
    event.listen(engine,'before_cursor_execute',record)
    try:
        for path in ['/naming-review','/naming-review?q=Current','/',f'/collections/{root}',f'/titles/{main}']:
            page=client.get(path)
            assert page.status_code==200
            assert 'Pojmenování' in page.text
        home=client.get('/').text
        assert f'collection_id={root}' in home and 'Pojmenování: kontrola' in home
        detail=client.get(f'/titles/{main}').text
        assert f'title_id={main}' in detail
    finally:event.remove(engine,'before_cursor_execute',record)
    assert not writes and snapshot(app)==before


def test_every_page_badge_count_and_link_match_the_central_queue(web):
    app,client,(root,main)=web
    with app.state.sessions() as session:
        plain_root=collection(session,'Plain');plain=title(session,plain_root,'Plain')
        plain.metadata_record.title_romaji='Plain'
        split_root=collection(session,'Split')
        p1=title(session,split_root,'P1',season=2,part=1,kind='part')
        title(session,split_root,'P2',season=2,part=2,kind='part')
        ncop=title(session,split_root,'NCOP',season=2,kind='bonus',metadata=False)
        for n,(owner,part) in enumerate(((plain_root,plain),(split_root,p1),(split_root,ncop))):
            session.add(Video(catalog_title=part,catalog_collection=owner,filename=f'V{n}.mkv',relative_path=f'{owner.local_title}/V{n}.mkv',
                              root_folder=owner.local_title,size=1,mtime_ns=1,season_episode_number=1))
        session.commit()
        collection_ids=(root,plain_root.id,split_root.id);title_ids=(main,plain.id,p1.id,ncop.id)
    api,service=review_api()
    with app.state.sessions() as session:
        central=api.build_naming_review(service.load_naming_review_context(session))
    def links(text):
        return [(html.unescape(href),re.sub(r'<[^>]+>','',label)) for href,label in re.findall(r'href="(/naming-review\?[^"]*)"[^>]*>(.*?)</a>',text,re.S)]
    def queue(href):
        return client.get(href).text.count('data-naming-owner=')
    home={int(dict(parse_qsl(urlsplit(href).query))['collection_id']):(href,label) for href,label in links(client.get('/').text)}
    assert set(home)==set(collection_ids)
    assert sum(central.collection_badges[id].review_count for id in collection_ids)==len(central.pending)>0
    for id in collection_ids:
        badge=central.collection_badges[id]
        assert links(client.get(f'/collections/{id}').text)==[home[id]]
        href,label=home[id]
        assert label.startswith(badge.label) and (' · %d'%badge.review_count in label)==bool(badge.review_count)
        assert queue(href)==badge.review_count if badge.state!='ok' else queue(href)>=1
    for id in title_ids:
        badge=central.title_badges[id]
        (href,label),=links(client.get(f'/titles/{id}').text)
        assert badge.label in label and href==badge.href
        assert queue(href)==badge.review_count if badge.state!='ok' else queue(href)>=1
    assert central.title_badges[ncop.id].state=='review' and central.collection_badges[plain_root.id].state=='ok'


def test_persisted_html_like_custom_text_is_escaped_by_the_application(web):
    app,client,(_,main)=web
    payload='<script>alert("naming")</script><img src=x onerror=alert(1)>'
    assert post(app,client,'title',main,'custom',custom_text=payload).status_code==303
    with app.state.sessions() as session:
        assert session.scalar(select(PhysicalNamingChoice)).physical_text==payload
    for path in ('/naming-review?status=confirmed',f'/naming-review?status=all&title_id={main}'):
        page=client.get(path).text
        assert '<script>alert(' not in page and '<img src=x' not in page
        assert str(escape(payload)) in page


@pytest.mark.parametrize('duplicate',['candidate_key','custom_text','fingerprint','return_to'])
def test_repeated_form_fields_are_rejected_without_choosing_one(web,duplicate):
    app,client,(_,main)=web
    row=unit(app,'title',main)
    fields=[('fingerprint',row.fingerprint),('candidate_key','custom'),('custom_text','Safe'),('return_to','/naming-review')]
    fields.append((duplicate,dict(fields)[duplicate]))
    before=snapshot(app)
    response=client.post(f'/naming-review/title/{main}/save',data=fields)
    assert response.status_code==400
    with app.state.sessions() as session:assert session.scalar(select(PhysicalNamingChoice)) is None
    assert snapshot(app)==before


def test_naming_query_count_is_bounded_as_units_and_candidates_grow(web):
    app,client,ids=web
    engine=app.state.sessions.kw['bind']
    def count():
        sql=[]
        def record(c,cursor,statement,p,context,many):sql.append(statement)
        event.listen(engine,'before_cursor_execute',record)
        try:assert client.get('/naming-review?status=all').status_code==200
        finally:event.remove(engine,'before_cursor_execute',record)
        return len(sql)
    baseline=count()
    with app.state.sessions() as session:
        for n in range(20):
            root=collection(session,f'Other {n}');main=title(session,root,f'Prefix {n}')
            main.metadata_record.synonyms_json=json.dumps([f'Alias {n}/{i}' for i in range(10)])
        session.commit()
    assert count()==baseline and baseline<=6


def test_readability_is_confirmable_and_inherited_child_is_not_an_extra_editor(web):
    app,client,(root,main)=web
    with app.state.sessions() as session:
        owner=session.get(CatalogTitle,main)
        owner.local_title='Season 1'
        owner.metadata_record.title_romaji='a'*71
        child=title(session,session.get(CatalogCollection,root),'NCOP',kind='bonus',metadata=False)
        session.commit();child_id=child.id
    page=client.get('/naming-review')
    assert 'Název je dlouhý. Zkontroluj fyzické pojmenování.' in page.text
    assert f'data-naming-owner="title:{child_id}"' not in page.text
    assert 'Tento název dědí' in page.text
    all_page=client.get('/naming-review?status=all')
    assert f'data-naming-owner="title:{child_id}"' in all_page.text
    assert f'action="/naming-review/title/{child_id}/save"' not in all_page.text
    assert post(app,client,'title',main,'romaji').status_code==303
    assert f'data-naming-owner="title:{main}"' not in client.get('/naming-review').text
    assert post(app,client,'title',child_id,'custom',custom_text='Fake inheritance choice').status_code==400
    with app.state.sessions() as session:
        assert session.scalar(select(PhysicalNamingChoice).where(PhysicalNamingChoice.catalog_title_id==child_id)) is None


def test_custom_preview_is_readonly_and_uses_known_filename_bytes(web):
    app,client,(_,main)=web
    row=unit(app,'title',main)
    before=snapshot(app)
    response=client.post(f'/naming-review/title/{main}/preview',data={'fingerprint':row.fingerprint,'custom_text':'a'*243})
    assert response.status_code==200
    preview=response.json()
    assert preview['utf8_bytes']==243 and preview['max_component_bytes']>255 and preview['valid'] is False
    assert '255' in ' '.join(preview['diagnostics'])
    assert snapshot(app)==before
    with app.state.sessions() as session:
        assert session.scalar(select(PhysicalNamingChoice)) is None


def test_invalid_saved_choice_can_reset_but_cannot_reconfirm(web):
    from app.physical_naming_service import confirm_physical_naming_choice
    app,client,(_,main)=web
    with app.state.sessions() as session:
        confirm_physical_naming_choice(session,session.get(CatalogTitle,main),'a'*243,'custom')
        session.commit()
    row=unit(app,'title',main)
    assert row.status=='problem'
    before=snapshot(app)
    assert post(app,client,'title',main,'reconfirm').status_code==400
    assert snapshot(app)==before
    assert post(app,client,'title',main,'reset').status_code==303


def test_reconfirm_parent_snapshot_after_context_change_keeps_historical_text(web):
    from app.physical_naming_service import confirm_physical_naming_choice
    app,client,(root,main)=web
    with app.state.sessions() as session:
        p1=session.get(CatalogTitle,main)
        p1.part_type=p1.part_type_manual='part';p1.part_number=p1.part_number_manual=1
        title(session,session.get(CatalogCollection,root),'P2',part=2,kind='part')
        child=title(session,session.get(CatalogCollection,root),'NCOP',part=None,kind='bonus',metadata=False)
        session.commit();child_id=child.id
    candidate=next(c for c in unit(app,'title',child_id).candidates if c.kind=='parent_prefix' and c.source_title_id==main)
    row=unit(app,'title',child_id)
    assert client.post(f'/naming-review/title/{child_id}/save',data={'fingerprint':row.fingerprint,'candidate_key':candidate.key}).status_code==303
    with app.state.sessions() as session:
        confirm_physical_naming_choice(session,session.get(CatalogTitle,main),'Renamed parent','custom')
        title(session,session.get(CatalogCollection,root),'P3',part=3,kind='part')
        session.commit()
    assert not unit(app,'title',child_id).resolution.basis_matches
    before=snapshot(app)
    assert post(app,client,'title',child_id,'reconfirm').status_code==303
    row=unit(app,'title',child_id)
    assert row.resolution.effective_text==candidate.raw_text and row.resolution.basis_matches
    with app.state.sessions() as session:
        choice=session.scalar(select(PhysicalNamingChoice).where(PhysicalNamingChoice.catalog_title_id==child_id))
        assert json.loads(choice.basis_snapshot_json)['source_title_id']==main
    assert snapshot(app)==before


@pytest.mark.parametrize('context_change',['own_metadata','owner_type'])
def test_historical_parent_snapshot_does_not_offer_ineligible_fresh_parents(web,context_change):
    app,client,(root,main)=web
    with app.state.sessions() as session:
        p1=session.get(CatalogTitle,main)
        p1.part_type=p1.part_type_manual='part';p1.part_number=p1.part_number_manual=1
        p2=title(session,session.get(CatalogCollection,root),'P2',part=2,kind='part')
        child=title(session,session.get(CatalogCollection,root),'NCOP',part=None,kind='bonus',metadata=False)
        session.commit();child_id,p2_id=child.id,p2.id
    old=unit(app,'title',child_id)
    selected=next(c for c in old.candidates if c.kind=='parent_prefix' and c.source_title_id==main)
    assert client.post(f'/naming-review/title/{child_id}/save',data={'fingerprint':old.fingerprint,'candidate_key':selected.key}).status_code==303
    with app.state.sessions() as session:
        child=session.get(CatalogTitle,child_id)
        if context_change=='own_metadata': attach_metadata(child,'child','Own supplementary Romaji')
        else:
            child.part_type=child.part_type_manual='part'
            child.part_number=child.part_number_manual=2
        session.commit()
    current=unit(app,'title',child_id)
    parents={c.source_title_id for c in current.candidates if c.kind=='parent_prefix' and c.source_title_id is not None}
    assert parents==set()
    before=snapshot(app)
    assert post(app,client,'title',child_id,'reconfirm').status_code==303
    with app.state.sessions() as session:
        choice=session.scalar(select(PhysicalNamingChoice).where(PhysicalNamingChoice.catalog_title_id==child_id))
        assert choice.physical_text==selected.raw_text and json.loads(choice.basis_snapshot_json)['source_title_id']==main
    assert snapshot(app)==before
    assert post(app,client,'title',child_id,'custom',custom_text='Explicit replacement').status_code==303
    if context_change=='own_metadata':
        assert post(app,client,'title',child_id,'english').status_code==303
        assert post(app,client,'title',child_id,'romaji').status_code==303
    assert snapshot(app)==before
