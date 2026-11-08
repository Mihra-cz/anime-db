"""LAN administration approves displayed canonical bytes with real ASGI/CSRF."""
from dataclasses import replace
import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.target_execution_manifest import manifest_to_json
from test_reorganization_approval import case, production, publish, snapshot, digest, accept


@pytest.fixture
def web(case, monkeypatch):
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    source = publish(case)
    from app import target_execution_mounts as mounts
    current = mounts._check_production_mount
    def local_mount(*args, **kwargs):
        record = current(*args, **kwargs)
        return record if kwargs['writable'] else replace(record, options=('ro',))
    monkeypatch.setattr(mounts, '_check_production_mount', local_mount)
    settings = Settings(anime_path=owner.root, database_url=f'sqlite:///{db}',
        metadata_artwork_directory=db.parent / 'artwork', metadata_download_artwork=False,
        reorganization_approval_inbox=inbox, reorganization_approval_outbox=outbox)
    app = create_app(settings)
    # No lifespan/startup or scanner; fixtures already own the isolated DB.
    client = TestClient(app, base_url='http://testserver')
    return case, client, source


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


def prepare(web):
    case, client, source = web
    api, owner, db, manifest, runtime, inbox, outbox, *_ = case
    path = f'/reorganization/approval/{source}'
    page = client.get(path)
    data = {'csrf': csrf(page), 'actor': 'operator', 'warnings': [w.warning_id for w in manifest.payload.warnings]}
    response = client.post(path + '/prepare', data=data, headers={'origin': 'http://testserver'})
    assert response.status_code == 200, response.text
    final = api.read_review(outbox, source)
    return path, response, final


def test_lan_administration_needs_no_login_and_requires_csrf(web):
    case, client, source = web
    path = f'/reorganization/approval/{source}'
    assert client.get(path).status_code == 200
    assert 'WWW-Authenticate' not in client.get(path).headers
    assert client.post(path + '/prepare', data={}).status_code == 403
    page = client.get(path)
    assert client.post(path + '/prepare', data={'csrf': csrf(page), 'actor': 'operator'},
        headers={'origin': 'https://attacker.test'}).status_code == 403


def test_exact_review_then_approval_preserves_db_and_media(web):
    case, client, source = web
    api, owner, db, manifest, runtime, inbox, outbox, ledger, lock = case
    before = snapshot(db, owner.root)
    path, page, final = prepare(web)
    assert digest(final) in page.text
    assert manifest.payload.mount_identity in page.text
    assert 'SOURCE' in page.text and 'TARGET' in page.text
    assert 'QUARANTINE' in page.text
    raw = client.get(path + '/manifest')
    assert raw.text == manifest_to_json(final)
    response = client.post(path + '/approve', data={'csrf': csrf(page), 'sha256': digest(final), 'confirm': 'yes', 'actor': 'operator'},
        headers={'origin': 'http://testserver'})
    assert response.status_code == 200
    assert 'Schváleno' in response.text
    assert 'Spuštěno: ne' in response.text
    assert api.read_approval(outbox, source)['approved_sha256'] == digest(final)
    assert snapshot(db, owner.root) == before
    again = client.post(path + '/approve', data={'csrf': csrf(response), 'sha256': digest(final), 'confirm': 'yes', 'actor': 'operator'},
        headers={'origin': 'http://testserver'})
    assert again.status_code == 409
    accept(case, source, final)
    status = client.get(path)
    assert 'Převzato hostitelem' in status.text
    assert 'Spuštěno: ne' in status.text


@pytest.mark.parametrize('fault', ['hash', 'stale_db', 'stale_fs', 'csrf', 'no_confirm'])
def test_approval_rejections_leave_sqlite_and_media_intact(web, fault):
    case, client, source = web
    api, owner, db, manifest, runtime, inbox, outbox, *_ = case
    path, page, final = prepare(web)
    data = {'csrf': csrf(page), 'sha256': digest(final), 'confirm': 'yes', 'actor': 'operator'}
    if fault == 'hash': data['sha256'] = '0' * 64
    elif fault == 'csrf': data['csrf'] = 'invalid'
    elif fault == 'no_confirm': data.pop('confirm')
    elif fault == 'stale_db':
        import sqlite3
        with sqlite3.connect(db) as connection:
            connection.execute('PRAGMA user_version=8')
    elif fault == 'stale_fs':
        (owner.root / manifest.payload.actions[0].source).write_bytes(b'stale')
    before = snapshot(db, owner.root)
    response = client.post(path + '/approve', data=data, headers={'origin': 'http://testserver'})
    assert response.status_code in (403, 409, 422)
    assert not (outbox / f'{source}.approval.json').exists()
    assert snapshot(db, owner.root) == before


def test_missing_warning_confirmation_is_stop(web):
    case, client, source = web
    path = f'/reorganization/approval/{source}'
    page = client.get(path)
    response = client.post(path + '/prepare', data={'csrf': csrf(page), 'actor': 'operator'}, headers={'origin': 'http://testserver'})
    assert response.status_code == 422
    assert not list(case[6].iterdir())


def test_web_validation_timeout_retains_no_approval(web, monkeypatch):
    import subprocess
    from app import reorganization_approval_web as module
    case, client, source = web
    path, page, final = prepare(web)
    monkeypatch.setattr(module, 'run_check', lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('check', 1)))
    response = client.post(path + '/approve', data={'csrf': csrf(page), 'sha256': digest(final), 'confirm': 'yes', 'actor': 'operator'},
        headers={'origin': 'http://testserver'})
    assert response.status_code == 504
    assert not (case[6] / f'{source}.approval.json').exists()


def test_no_authentication_configuration_is_needed(tmp_path):
    from app.reorganization_approval_web import configured
    assert configured(Settings(anime_path=tmp_path / 'media', database_url='sqlite://',
        reorganization_approval_inbox=tmp_path / 'inbox', reorganization_approval_outbox=tmp_path / 'outbox'))


def test_spool_cannot_be_inside_media(tmp_path):
    from app.reorganization_approval_web import configured
    assert not configured(Settings(anime_path=tmp_path / 'media', database_url='sqlite://',
        reorganization_approval_inbox=tmp_path / 'inbox', reorganization_approval_outbox=tmp_path / 'media' / 'outbox'))


def test_csrf_is_bound_to_source_and_manifest_sha(web):
    case, client, source = web
    api, owner, db, manifest, runtime, inbox, outbox, *_ = case
    path, page, final = prepare(web)
    other_original = replace(manifest, created_at='2026-10-19T11:59:59Z')
    from test_v6_production import SHARE
    other = api.publish_proposal(inbox=inbox, database=db, library_root=owner.root,
        share=SHARE, share_root='/', manifest=other_original, runtime=runtime)
    foreign = client.get(f'/reorganization/approval/{other}')
    response = client.post(path + '/approve', data={'csrf': csrf(foreign), 'sha256': digest(final),
        'confirm': 'yes', 'actor': 'operator'}, headers={'origin': 'http://testserver'})
    assert response.status_code == 403
    assert not (outbox / f'{source}.approval.json').exists()


def test_approval_does_not_offer_execution_or_import_routes(web):
    case, client, source = web
    paths = {r.path for r in client.app.routes}
    assert f'/reorganization/approval/{{source}}/approve' in paths
    assert not any(p.startswith('/reorganization') and p.endswith(('/execute', '/start', '/run')) for p in paths)
    response = client.post(f'/reorganization/approval/{source}/execute', data={})
    assert response.status_code == 404


def test_get_and_download_never_inventory_or_mutate(web, monkeypatch):
    from app import reorganization_approval_web as module
    case, client, source = web
    before = snapshot(case[2], case[1].root)
    monkeypatch.setattr(module, 'run_check', lambda *a, **k: pytest.fail('GET started filesystem inventory'))
    assert client.get('/reorganization/approval').status_code == 200
    assert client.get(f'/reorganization/approval/{source}').status_code == 200
    assert client.get(f'/reorganization/approval/{source}/manifest').text == manifest_to_json(case[3])
    assert snapshot(case[2], case[1].root) == before
    assert not list(case[6].iterdir())


def test_display_preserves_nanosecond_evidence_without_javascript_rounding():
    from app.reorganization_approval_web import as_view
    from test_v6_execution_manifest import make, fixture
    from dataclasses import replace
    ctx, snap = fixture()
    exact = 1794136596822269301
    snap = replace(snap, entries=(replace(snap.entries[0], mtime_ns=exact),))
    manifest = make(ctx, snap)
    assert as_view(manifest)['payload']['actions'][0]['expected_mtime_ns'] == str(exact)
    assert manifest.payload.actions[0].expected_mtime_ns == exact


def test_expired_proposal_has_explicit_stop_and_no_approval_button(web):
    import json
    from test_naming_review_web import EndpointClient
    case, client, source = web
    api, owner, db, manifest, runtime, inbox, outbox, *_ = case
    bundle = api.read_proposal(inbox, source)
    final = api.prepare_review(bundle, actor='operator', warning_ids=[w.warning_id for w in manifest.payload.warnings])
    api.write_review(outbox, source, final)
    bundle['published_at'] = '2020-01-01T00:00:00+00:00'
    (inbox / f'{source}.proposal.json').write_text(json.dumps(bundle))
    page = EndpointClient(client.app).get(f'/reorganization/approval/{source}')
    assert page.status_code == 200
    assert 'STOP: hostitelská evidence vypršela' in page.text
    assert '>Schválit tento canonical manifest</button>' not in page.text


def test_non_ascii_csrf_is_rejected_cleanly(web):
    from test_naming_review_web import EndpointClient
    case, client, source = web
    direct = EndpointClient(client.app)
    path = f'/reorganization/approval/{source}'
    token = csrf(direct.get(path))
    response = direct.post(path + '/prepare', data={'csrf': token[:-1] + 'é', 'actor': 'operator'},
        headers={'origin': 'http://testserver', 'cookie': f'reorganization_csrf={token}'})
    assert response.status_code == 403
    assert not list(case[6].iterdir())


def test_approval_pages_allow_same_origin_native_form_posts(web):
    case, client, source = web
    assert client.get(f'/reorganization/approval/{source}').headers['referrer-policy'] == 'same-origin'
    _, response, _ = prepare(web)
    assert response.headers['referrer-policy'] == 'same-origin'


@pytest.fixture
def warning_free_web(production, tmp_path, monkeypatch):
    from app import reorganization_approval as api
    owner, db, manifest, runtime = production
    assert not manifest.payload.warnings
    inbox, outbox, ledger = (tmp_path / name for name in ('inbox', 'outbox', 'ledger'))
    for directory in (inbox, outbox, ledger):
        directory.mkdir(mode=0o700)
    lock = db.parent / 'runtime.lock'
    lock.write_bytes(b'')
    lock.chmod(0o600)
    return web.__wrapped__((api, owner, db, manifest, runtime, inbox, outbox, ledger, lock), monkeypatch)


def test_warning_free_forms_keep_entered_actor_in_receipt(warning_free_web):
    import html
    case, client, source = warning_free_web
    api, owner, db, manifest, runtime, inbox, outbox, *_ = case
    before = snapshot(db, owner.root)
    path = f'/reorganization/approval/{source}'
    response = client.post(path + '/prepare', data={'csrf': csrf(client.get(path)), 'actor': 'Mirek Local'},
        headers={'origin': 'http://testserver'})
    assert response.status_code == 200
    actor = html.unescape(re.search(r'type="hidden" name="actor" value="([^"]+)"', response.text)[1])
    assert actor == 'Mirek Local'
    final = api.read_review(outbox, source)
    approved = client.post(path + '/approve', data={'csrf': csrf(response), 'sha256': digest(final),
        'actor': actor, 'confirm': 'yes'}, headers={'origin': 'http://testserver'})
    assert approved.status_code == 200
    assert api.read_approval(outbox, source)['actor'] == 'Mirek Local'
    duplicate = client.post(path + '/approve', data={'csrf': csrf(approved), 'sha256': digest(final),
        'actor': actor, 'confirm': 'yes'}, headers={'origin': 'http://testserver'})
    assert duplicate.status_code == 409
    assert len(list(outbox.glob('*.approval.json'))) == 1
    assert accept(case, source, final, actor='Mirek Local')['started'] is False
    assert snapshot(db, owner.root) == before


def test_failed_inspect_logs_bounded_escaped_stderr(web, monkeypatch, caplog):
    from app import reorganization_approval_web as module
    case, client, source = web
    class FailedInspect:
        returncode = 7
        def communicate(self, _input, *, timeout):
            return b'', b'OMITTED_PREFIX' + b'x' * 4096 + b'\r\n\x1b[31m\xff'
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs: FailedInspect())
    with caplog.at_level('WARNING', logger=module.__name__):
        with pytest.raises(ValueError, match='STOP'):
            module.run_check(case[2], case[1].root, case[5], source, case[3], timeout=1)
    messages = [r.getMessage() for r in caplog.records if r.name == module.__name__]
    assert len(messages) == 1
    assert '7' in messages[0] and r'\r\n\x1b[31m\xff' in messages[0]
    assert 'OMITTED_PREFIX' not in messages[0]
    assert '\n' not in messages[0] and '\r' not in messages[0] and '\x1b' not in messages[0]
    assert len(messages[0]) < 8400
    assert not list(case[6].iterdir())


@pytest.mark.parametrize('operation', ['prepare', 'approve'])
@pytest.mark.parametrize('blocking_work', ['load', 'render'])
def test_other_routes_remain_available_during_approval_io(web, monkeypatch, operation, blocking_work):
    import asyncio
    import threading
    import httpx
    from app import reorganization_approval as api
    from app.main import templates
    case, client, source = web
    path = f'/reorganization/approval/{source}'
    page = client.get(path)
    if operation == 'prepare':
        data = {'csrf': csrf(page), 'actor': 'operator', 'warnings': [w.warning_id for w in case[3].payload.warnings]}
    else:
        path, page, final = prepare(web)
        data = {'csrf': csrf(page), 'actor': 'operator', 'sha256': digest(final), 'confirm': 'yes'}
    entered, release, active = threading.Event(), threading.Event(), threading.Event()
    target, name = (api, 'read_proposal') if blocking_work == 'load' else (templates, 'TemplateResponse')
    original = getattr(target, name)
    def slow_once(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            active.set()
            try:
                release.wait(2)
            finally:
                active.clear()
        return original(*args, **kwargs)
    monkeypatch.setattr(target, name, slow_once)
    before = snapshot(case[2], case[1].root)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url='http://testserver', follow_redirects=True) as concurrent:
            task = asyncio.create_task(concurrent.post(path + '/' + operation, data=data,
                headers={'origin': 'http://testserver', 'cookie': f'reorganization_csrf={csrf(page)}'}))
            try:
                while not entered.is_set():
                    await asyncio.sleep(0.005)
                assert active.is_set(), 'event loop stayed blocked until slow I/O finished'
                response = await asyncio.wait_for(concurrent.get('/health'), timeout=0.5)
                assert response.status_code == 200
                assert active.is_set(), 'health request waited for approval I/O'
            finally:
                release.set()
                submitted = await task
            assert submitted.status_code == 200, submitted.text
    asyncio.run(exercise())
    assert snapshot(case[2], case[1].root) == before


@pytest.mark.parametrize('operation', ['prepare', 'approve'])
@pytest.mark.parametrize('fault', ['null_origin', 'foreign_origin', 'missing_origin', 'missing_csrf', 'mismatched_csrf_cookie'])
def test_both_form_posts_reject_invalid_origin_or_csrf(web, operation, fault):
    case, client, source = web
    path = f'/reorganization/approval/{source}'
    page = client.get(path)
    if operation == 'prepare':
        data = {'csrf': csrf(page), 'actor': 'operator', 'warnings': [w.warning_id for w in case[3].payload.warnings]}
    else:
        path, page, final = prepare(web)
        data = {'csrf': csrf(page), 'actor': 'operator', 'sha256': digest(final), 'confirm': 'yes'}
    headers = {'origin': 'http://testserver'}
    if fault == 'null_origin': headers['origin'] = 'null'
    elif fault == 'foreign_origin': headers['origin'] = 'https://attacker.test'
    elif fault == 'missing_origin': headers.pop('origin')
    elif fault == 'missing_csrf': data.pop('csrf')
    elif fault == 'mismatched_csrf_cookie': headers['cookie'] = 'reorganization_csrf=wrong'
    before = snapshot(case[2], case[1].root)
    assert client.post(path + '/' + operation, data=data, headers=headers).status_code == 403
    assert not list(case[6].glob('*.approval.json'))
    assert snapshot(case[2], case[1].root) == before
