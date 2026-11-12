"""LAN administration approval. No login, mount, Docker or executor access."""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response, RedirectResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import reorganization_approval as approval
from .reorganization_web import database_path, display_safe, _stop_worker
from .target_execution_manifest import manifest_from_json, manifest_to_json

COOKIE = 'reorganization_csrf'
logger = logging.getLogger(__name__)


def browser_session(request):
    """One browser cookie; each form has its own signed source/SHA binding."""
    import re
    session = request.cookies.get(COOKIE, '')
    return session if re.fullmatch('[a-f0-9]{64}', session) else secrets.token_hex(32)


def configured(settings):
    inbox, outbox = settings.reorganization_approval_inbox, settings.reorganization_approval_outbox
    if inbox is None or outbox is None:
        return False
    inbox, outbox = Path(inbox), Path(outbox)
    media = settings.anime_path.resolve()
    return (inbox.is_absolute() and outbox.is_absolute()
            and not inbox.resolve().is_relative_to(media)
            and not outbox.resolve().is_relative_to(media)
            and not inbox.resolve().is_relative_to(outbox.resolve())
            and not outbox.resolve().is_relative_to(inbox.resolve()))


def run_check(database, library_root, inbox, source, final, *, timeout):
    """Fixed RO helper, bounded like the existing preview. No user-supplied command."""
    process = subprocess.Popen([sys.executable, '-B', '-m', 'app.tools.reorganization_approval', 'inspect',
        '--db', str(database), '--library-root', str(library_root), '--inbox', str(inbox),
        '--source-sha256', approval.identity(source)], cwd=Path(__file__).resolve().parent.parent,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(manifest_to_json(final).encode('utf-8'), timeout=timeout)
    except BaseException as exc:
        _stop_worker(process)
        if isinstance(exc, subprocess.TimeoutExpired):
            exc.worker = process
        raise
    if process.returncode:
        diagnostic = repr(stderr[-2048:])[1:-1]
        logger.warning('Reorganization inspect worker failed (exit %s): %s', process.returncode, diagnostic)
        raise ValueError('STOP: ověření manifestu selhalo nebo hostitelská evidence vypršela.')
    return json.loads(stdout)


def install_approval_routes(app, templates, settings):
    inbox, outbox = settings.reorganization_approval_inbox, settings.reorganization_approval_outbox
    secret = secrets.token_bytes(32)
    timeout = settings.reorganization_preview_timeout_seconds

    def administration(request):
        # All current AnimeDB routes are trusted-LAN administration by explicit
        # product decision. Keep this boundary ready for future access control.
        if not configured(settings):
            raise HTTPException(503, 'Schvalování není nakonfigurováno.')

    def actor_value(value):
        value = value.strip()
        if not value or len(value) > 128 or any(ord(c) < 32 for c in value):
            raise HTTPException(422, 'Uveďte jméno správce pro evidenci schválení.')
        return value

    def token(source, digest, session):
        value = f'{int(time.time())}.{secrets.token_hex(16)}.{source}.{digest}'
        return value + '.' + hmac.new(secret, (value + '.' + session).encode(), hashlib.sha256).hexdigest()

    def preview_authorization(preview, digest, request):
        coverage, plan = preview['coverage'], preview['plan']
        if (not coverage['inventory_complete'] or not coverage['files_complete']
                or not plan['records'] or plan['collisions']
                or any(row['status'] in {'BLOCKED', 'REVIEW'} for row in plan['records'])):
            return None
        request_id = secrets.token_hex(32)
        session = browser_session(request)
        return {'request_id': request_id, 'preview_db_sha256': digest,
            'csrf': token(request_id, digest, session), 'session': session}

    app.state.reorganization_preview_authorization = preview_authorization

    async def form(request, source=None, digest=None):
        if request.headers.get('origin') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Vyžadován stejný Origin.')
        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise HTTPException(403, 'Vyžadován schvalovací formulář.')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                raise HTTPException(413, 'Formulář je příliš velký.')
        try:
            fields = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=20000)
            if source is None:
                source = approval.identity(fields.get('request_id', [''])[0])
                digest = approval.identity(fields.get('preview_db_sha256', [''])[0])
            csrf = fields.get('csrf', [''])[0]
            value, signature = csrf.rsplit('.', 1)
            timestamp, nonce, bound_source, bound_digest = value.split('.')
            session = request.cookies.get(COOKIE, '')
            expected = hmac.new(secret, (value + '.' + session).encode(), hashlib.sha256).hexdigest()
            valid = (len(session) == 64
                     and secrets.compare_digest(signature, expected) and bound_source == source
                     and bound_digest == digest and len(nonce) == 32
                     and 0 <= time.time() - int(timestamp) <= approval.MAX_AGE_SECONDS)
        except (ValueError, UnicodeError, TypeError):
            valid = False
        if not valid:
            raise HTTPException(403, 'Neplatná nebo vypršelá CSRF ochrana; znovu otevřete manifest.')
        if any(len(values) != 1 for key, values in fields.items() if key != 'warnings'):
            raise HTTPException(422, 'Nejednoznačný formulář.')
        return fields

    def write_request(kind, request_id, record):
        try:
            approval.publish_record(outbox, approval.identity(request_id) + '.' + kind + '-request.json',
                                  json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=True))
        except FileExistsError:
            raise HTTPException(409, 'Požadavek již existuje. Neopakujte jej; sledujte jeho stav.')
        except (OSError, ValueError):
            raise HTTPException(503, 'STOP: požadavek se nepodařilo bezpečně uložit. Neopakujte jej automaticky.')

    def load(source):
        try:
            bundle = approval.read_proposal(inbox, source)
            original = manifest_from_json(bundle['manifest'])
            try:
                final = approval.read_review(outbox, source)
                approval.validate_review(bundle, final)
            except FileNotFoundError:
                final = None
            return bundle, original, final
        except (ValueError, OSError, TypeError, KeyError):
            raise HTTPException(409, 'STOP: platný původní hostitelský manifest nebo review není dostupné.')

    def fresh(source, final, phase):
        lock = app.state.reorganization_preview_lock
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'Jiné ověření nebo náhled právě probíhá.')
        started = time.monotonic()
        try:
            previous = app.state.reorganization_stopping_worker
            if previous is not None and previous.poll() is None:
                raise HTTPException(409, 'Předchozí ověření se ještě ukončuje.')
            app.state.reorganization_stopping_worker = None
            result = run_check(database_path(settings.database_url), settings.anime_path, inbox, source,
                               final, timeout=timeout)
            if result['outcome'] != 'READY_FOR_EXECUTION':
                raise HTTPException(409, 'STOP: preflight ' + result['outcome'] + ': ' + ', '.join(result['diagnostics']))
            approval.require_current_proposal(approval.read_proposal(inbox, source))
            return result
        except subprocess.TimeoutExpired as exc:
            app.state.reorganization_stopping_worker = getattr(exc, 'worker', None)
            raise HTTPException(504, 'STOP: ověření překročilo časový limit; schválení nevzniklo.')
        except (ValueError, OSError, subprocess.CalledProcessError):
            raise HTTPException(409, 'STOP: ověření manifestu selhalo; schválení nevzniklo.')
        finally:
            logger.info('V6 timing phase=%s source=%s elapsed_seconds=%.3f', phase, source, time.monotonic() - started)
            lock.release()

    def render(request, source):
        from .reorganization_presentation import catalog, describe, LABELS
        bundle, original, final = load(source)
        manifest = final or original
        digest = approval.sha256(manifest)
        actors = {a.actor for a in manifest.acknowledgements}
        if len(actors) > 1:
            raise HTTPException(409, 'STOP: nejednoznačný správce review.')
        try:
            actor = approval.read_review_actor(outbox, source, final) if final is not None else 'administrator'
        except (ValueError, OSError):
            raise HTTPException(409, 'STOP: správce review není bezpečně doložen; připravte nový původní manifest.')
        server_now = datetime.now(timezone.utc)
        age = (server_now - datetime.fromisoformat(bundle['published_at'])).total_seconds()
        current = 0 <= age <= approval.MAX_AGE_SECONDS
        expires_at = datetime.fromisoformat(bundle['published_at']) + timedelta(seconds=approval.MAX_AGE_SECONDS)
        remaining = max(0, int((expires_at - server_now).total_seconds()))
        try:
            receipt = approval.read_approval(outbox, source)
            if (final is None or receipt.get('approved_sha256') != digest or (actors and receipt.get('actor') != actor)
                    or receipt.get('source_sha256') != source or receipt.get('state') != 'APPROVED'):
                raise ValueError('approval differs')
        except FileNotFoundError:
            receipt = None
        except (ValueError, OSError, AttributeError):
            raise HTTPException(409, 'STOP: neplatný schvalovací záznam.')
        try:
            accepted = json.loads(approval.read_record(inbox, source + '.acceptance.json'))
            if (receipt is None or accepted.get('approved_sha256') != digest
                    or accepted.get('state') not in {'ACCEPTED_EXECUTION_DISABLED', 'EXECUTION_CLAIMED'} or accepted.get('started') is not False):
                raise ValueError('acceptance differs')
        except FileNotFoundError:
            accepted = None
        except (ValueError, OSError, AttributeError):
            raise HTTPException(409, 'STOP: neplatné hostitelské převzetí.')
        try:
            execution_result = approval.read_execution_result(inbox, source, manifest)
        except FileNotFoundError:
            execution_result = None
        except (ValueError, OSError, TypeError, KeyError):
            raise HTTPException(409, 'STOP: výsledek execution neodpovídá schválenému manifestu.')
        if receipt is not None:
            actor = receipt['actor']
        try:
            start_requested = json.loads(approval.read_record(outbox, source + '.start-request.json'))
            if (start_requested.get('source_sha256') != source or start_requested.get('approved_sha256') != digest
                    or start_requested.get('manifest_id') != manifest.manifest_id or start_requested.get('kind') != 'start'):
                raise ValueError('start request differs')
        except FileNotFoundError:
            start_requested = None
        except (ValueError, OSError, AttributeError):
            raise HTTPException(409, 'STOP: neplatný požadavek na spuštění.')
        coordinator_status = None
        if start_requested is not None:
            coordinator_status = job_status(source, 'start')
        session = browser_session(request)
        csrf = token(source, digest, session)
        response = templates.TemplateResponse(request, 'reorganization_approval.html', display_safe({
            'source': source, 'manifest': as_view(manifest), 'canonical_sha256': digest,
            'presentation_catalog': catalog(), 'presentation_labels': LABELS, 'describe': describe,
            'canonical': manifest_to_json(manifest), 'bundle': bundle, 'reviewed': final is not None,
            'receipt': receipt, 'accepted': accepted, 'csrf': csrf, 'actor': actor, 'result': None,
            'execution_result': execution_result,
            'current': current,
            'start_requested': start_requested,
            'coordinator_status': coordinator_status,
            'expires_at': expires_at.isoformat().replace('+00:00', 'Z'),
            'remaining_seconds': remaining,
            'server_now': server_now.isoformat().replace('+00:00', 'Z'),
        }))
        response.set_cookie(COOKIE, session, max_age=approval.MAX_AGE_SECONDS, httponly=True,
                            secure=request.url.scheme == 'https', samesite='strict', path='/reorganization')
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
                                 'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff'})
        return response

    def stop_page(request, error):
        from .reorganization_presentation import explain_error
        detail = str(error.detail)
        summary, explanations = explain_error(detail)
        return templates.TemplateResponse(request, 'reorganization_stop.html', {
            'detail': detail, 'summary': summary, 'status_code': error.status_code, 'explanations': explanations,
        }, status_code=error.status_code, headers={
            **(error.headers or {}), 'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
            'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff'})

    def show(request, action):
        try:
            administration(request)
            return action()
        except HTTPException as error:
            return stop_page(request, error)

    def job_status(request_id, kind):
        if kind not in {'prepare', 'start'}:
            raise HTTPException(404, 'Neznámý druh požadavku.')
        try:
            request_id = approval.identity(request_id)
            record = json.loads(approval.read_record(outbox, request_id + '.' + kind + '-request.json'))
            if record.get('version') != 1 or record.get('request_id') != request_id or record.get('kind') != kind:
                raise ValueError('request identity differs')
            try:
                state = json.loads(approval.read_record(inbox, request_id + '.' + kind + '-status.json'))
            except FileNotFoundError:
                return {'version': 1, 'request_id': request_id, 'kind': kind, 'state': 'PENDING',
                        'phase': 'queued', 'detail': 'Čeká na hostitelský koordinátor. Spuštění ani připravenost nejsou potvrzené.',
                        'source_sha256': request_id if kind == 'start' else None, 'result_url': None}
            if (type(state) is not dict or state.get('version') != 1 or state.get('request_id') != request_id
                    or state.get('kind') != kind or state.get('state') not in {'RUNNING', 'READY', 'NORMAL', 'STOP'}
                    or any(type(state.get(key)) is not str for key in ('phase', 'detail'))):
                raise ValueError('status identity differs')
            source = state.get('source_sha256')
            if source is not None:
                approval.identity(source)
            if kind == 'start' and source != request_id:
                raise ValueError('source identity differs')
            result_url = None
            if state['state'] == 'READY' and kind == 'prepare':
                approval.read_proposal(inbox, source)
                result_url = '/reorganization/approval/' + source
            elif kind == 'start' and state['state'] == 'NORMAL':
                result_url = '/reorganization/approval/' + request_id
            return {**state, 'result_url': result_url}
        except FileNotFoundError:
            raise HTTPException(404, 'Požadavek není dostupný.')
        except (ValueError, OSError, TypeError, AttributeError, KeyError):
            raise HTTPException(409, 'STOP: neplatný stav hostitelského požadavku; žádný automatický retry.')

    def job_page(request, request_id, kind='prepare'):
        state = job_status(request_id, kind)
        return templates.TemplateResponse(request, 'reorganization_job.html', {
            'job': state, 'status_url': '/reorganization/jobs/' + request_id + '/status?kind=' + kind,
        }, headers={'Cache-Control': 'no-store'})

    @app.post('/reorganization/prepare', response_class=HTMLResponse)
    async def request_manifest(request: Request):
        try:
            administration(request)
            fields = await form(request)
            request_id, digest = fields['request_id'][0], fields['preview_db_sha256'][0]
            actor = actor_value(fields.get('actor', [''])[0])
            from .tools.target_plan import database_fingerprint
            current = await run_in_threadpool(database_fingerprint, database_path(settings.database_url))
            if current['sha256'] != digest:
                raise HTTPException(409, 'Náhled databáze zastaral. Připravte aktuální náhled.')
            await run_in_threadpool(write_request, 'prepare', request_id, {
                'version': 1, 'kind': 'prepare', 'request_id': request_id, 'actor': actor,
                'requested_at': approval.now(), 'preview_db_sha256': digest})
            return RedirectResponse('/reorganization/jobs/' + request_id, status_code=303)
        except HTTPException as error:
            return await run_in_threadpool(stop_page, request, error)
        except (OSError, ValueError):
            return await run_in_threadpool(stop_page, request, HTTPException(503, 'Databáze není dostupná pro ověření náhledu.'))

    @app.get('/reorganization/jobs/{request_id}', response_class=HTMLResponse)
    def job(request: Request, request_id: str):
        return show(request, lambda: job_page(request, request_id))

    @app.get('/reorganization/jobs/{request_id}/status')
    def status(request: Request, request_id: str, kind: str = 'prepare'):
        administration(request)
        return JSONResponse(job_status(request_id, kind), headers={'Cache-Control': 'no-store'})

    @app.get('/_service/workflow', response_class=HTMLResponse)
    def direct_workflow(request: Request, source: str):
        # The deployed edge serves this URL even while this process is stopped.
        return show(request, lambda: job_page(request, source, 'start'))

    def list_proposals(request):
        try:
            names = sorted(p.name[:64] for p in Path(inbox).iterdir()
                           if p.name.endswith('.proposal.json') and len(p.name) == 78)
            for name in names:
                approval.identity(name)
        except (ValueError, OSError):
            raise HTTPException(503, 'Hostitelská schvalovací schránka není dostupná.')
        return templates.TemplateResponse(request, 'reorganization_proposals.html', {'sources': names},
                                          headers={'Cache-Control': 'no-store'})

    @app.get('/reorganization/approval', response_class=HTMLResponse)
    def proposals(request: Request):
        return show(request, lambda: list_proposals(request))

    @app.get('/reorganization/approval/{source}', response_class=HTMLResponse)
    def proposal(request: Request, source: str):
        return show(request, lambda: render(request, source))

    @app.get('/reorganization/approval/{source}/manifest')
    def download(request: Request, source: str):
        def canonical_response():
            _, original, final = load(source)
            return Response(manifest_to_json(final or original), media_type='application/json',
                headers={'Cache-Control': 'no-store', 'Content-Disposition': 'attachment; filename="execution-manifest.json"'})
        return show(request, canonical_response)

    def load_request(request, source):
        administration(request)
        bundle, original, final = load(source)
        return bundle, original, final, approval.sha256(final or original)

    def save_decision(command, source, bundle, original, existing, fields):
        actor = actor_value(fields.get('actor', [''])[0])
        if command == 'prepare':
            if existing is not None:
                raise HTTPException(409, 'Review už existuje; nelze je přepsat ani znovu připravit.')
            try:
                final = approval.prepare_review(bundle, actor=actor, warning_ids=fields.get('warnings', []))
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            fresh(source, final, 'warning_preflight')
            try:
                approval.write_review(outbox, source, final, actor=actor)
            except FileExistsError:
                raise HTTPException(409, 'Review už existuje.')
            except (ValueError, OSError):
                raise HTTPException(503, 'STOP: review se nepodařilo bezpečně uložit; neopakovat automaticky.')
            return
        if existing is None:
            raise HTTPException(409, 'Nejprve připravte přesný canonical manifest k review.')
        if fields.get('sha256') != [approval.sha256(existing)] or fields.get('confirm') != ['yes']:
            raise HTTPException(422, 'Vyžadováno výslovné schválení zobrazeného SHA-256.')
        try:
            approval.validate_review(bundle, existing, actor=actor)
            if actor != approval.read_review_actor(outbox, source, existing):
                raise ValueError('review actor differs')
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc))
        if command == 'start':
            try:
                receipt = approval.read_approval(outbox, source)
                if (type(receipt) is not dict or receipt.get('state') != 'APPROVED'
                        or receipt.get('source_sha256') != source or receipt.get('approved_sha256') != approval.sha256(existing)
                        or receipt.get('manifest_id') != existing.manifest_id or receipt.get('actor') != actor):
                    raise ValueError('exact approval required')
                for name in (source + '.acceptance.json', source + '.result.json'):
                    try:
                        approval.read_record(inbox, name)
                    except FileNotFoundError:
                        continue
                    raise ValueError('approval already spent; no retry')
            except (ValueError, OSError):
                raise HTTPException(409, 'STOP: vyžadováno platné, dosud nepřevzaté jednorázové schválení.')
            fresh(source, existing, 'start_preflight')
            write_request('start', source, {'version': 1, 'kind': 'start', 'request_id': source,
                'source_sha256': source, 'approved_sha256': approval.sha256(existing),
                'manifest_id': existing.manifest_id, 'actor': actor, 'requested_at': approval.now()})
            return
        fresh(source, existing, 'approval_preflight')
        try:
            approval.write_approval(outbox, source, existing, actor=actor, approved_sha256=fields['sha256'][0])
        except FileExistsError:
            raise HTTPException(409, 'Manifest byl již jednorázově schválen.')
        except (ValueError, OSError):
            raise HTTPException(503, 'STOP: schválení se nepodařilo bezpečně uložit; neopakovat automaticky.')

    async def submit(request, source, command):
        try:
            bundle, original, final, digest = await run_in_threadpool(load_request, request, source)
            fields = await form(request, source, digest)
            await run_in_threadpool(save_decision, command, source, bundle, original, final, fields)
            if command == 'start':
                return RedirectResponse('/_service/workflow?source=' + source, status_code=303)
            return RedirectResponse('/reorganization/approval/' + source, status_code=303)
        except HTTPException as error:
            return await run_in_threadpool(stop_page, request, error)

    @app.post('/reorganization/approval/{source}/prepare', response_class=HTMLResponse)
    async def prepare(request: Request, source: str):
        return await submit(request, source, 'prepare')

    @app.post('/reorganization/approval/{source}/approve', response_class=HTMLResponse)
    async def approve(request: Request, source: str):
        return await submit(request, source, 'approve')

    @app.post('/reorganization/approval/{source}/start', response_class=HTMLResponse)
    async def start(request: Request, source: str):
        return await submit(request, source, 'start')


def as_view(manifest):
    from dataclasses import asdict
    view = asdict(manifest)
    # Nanosecond evidence exceeds JavaScript's safe integer range. Only the
    # presentation data uses strings; the approved canonical JSON stays exact.
    for action in view['payload']['actions']:
        action['expected_mtime_ns'] = str(action['expected_mtime_ns'])
        action['expected_size'] = str(action['expected_size'])
    return view
