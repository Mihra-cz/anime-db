"""LAN administration approval. No login, mount, Docker or executor access."""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from . import reorganization_approval as approval
from .reorganization_web import database_path, display_safe, _stop_worker
from .target_execution_manifest import manifest_from_json, manifest_to_json

COOKIE = 'reorganization_csrf'
logger = logging.getLogger(__name__)


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
            raise HTTPException(503, 'Schvalování není nakonfigurováno; fyzické spuštění je deaktivované.')

    def actor_value(value):
        value = value.strip()
        if not value or len(value) > 128 or any(ord(c) < 32 for c in value):
            raise HTTPException(422, 'Uveďte jméno správce pro evidenci schválení.')
        return value

    def token(source, digest):
        value = f'{int(time.time())}.{secrets.token_hex(16)}.{source}.{digest}'
        return value + '.' + hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()

    async def form(request, source, digest):
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
            csrf = fields.get('csrf', [''])[0]
            value, signature = csrf.rsplit('.', 1)
            timestamp, nonce, bound_source, bound_digest = value.split('.')
            expected = hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()
            valid = (secrets.compare_digest(csrf, request.cookies.get(COOKIE, ''))
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

    def fresh(source, final):
        lock = app.state.reorganization_preview_lock
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'Jiné ověření nebo náhled právě probíhá.')
        try:
            previous = app.state.reorganization_stopping_worker
            if previous is not None and previous.poll() is None:
                raise HTTPException(409, 'Předchozí ověření se ještě ukončuje.')
            app.state.reorganization_stopping_worker = None
            result = run_check(database_path(settings.database_url), settings.anime_path, inbox, source,
                               final, timeout=timeout)
            if result['outcome'] != 'READY_FOR_EXECUTION':
                raise HTTPException(409, 'STOP: preflight ' + result['outcome'] + ': ' + ', '.join(result['diagnostics']))
            return result
        except subprocess.TimeoutExpired as exc:
            app.state.reorganization_stopping_worker = getattr(exc, 'worker', None)
            raise HTTPException(504, 'STOP: ověření překročilo časový limit; schválení nevzniklo.')
        except (ValueError, OSError, subprocess.CalledProcessError):
            raise HTTPException(409, 'STOP: ověření manifestu selhalo; schválení nevzniklo.')
        finally:
            lock.release()

    def render(request, source, *, result=None, prepared_actor=None):
        bundle, original, final = load(source)
        manifest = final or original
        digest = approval.sha256(manifest)
        actors = {a.actor for a in manifest.acknowledgements}
        if len(actors) > 1:
            raise HTTPException(409, 'STOP: nejednoznačný správce review.')
        actor = next(iter(actors), prepared_actor or 'administrator')
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(bundle['published_at'])).total_seconds()
        current = 0 <= age <= approval.MAX_AGE_SECONDS
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
                    or accepted.get('state') != 'ACCEPTED_EXECUTION_DISABLED' or accepted.get('started') is not False):
                raise ValueError('acceptance differs')
        except FileNotFoundError:
            accepted = None
        except (ValueError, OSError, AttributeError):
            raise HTTPException(409, 'STOP: neplatné hostitelské převzetí.')
        if receipt is not None:
            actor = receipt['actor']
        csrf = token(source, digest)
        response = templates.TemplateResponse(request, 'reorganization_approval.html', display_safe({
            'source': source, 'manifest': as_view(manifest), 'canonical_sha256': digest,
            'canonical': manifest_to_json(manifest), 'bundle': bundle, 'reviewed': final is not None,
            'receipt': receipt, 'accepted': accepted, 'csrf': csrf, 'actor': actor, 'result': result,
            'current': current,
        }))
        response.set_cookie(COOKIE, csrf, max_age=approval.MAX_AGE_SECONDS, httponly=True,
                            secure=request.url.scheme == 'https', samesite='strict', path='/reorganization/approval')
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
                                 'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff'})
        return response

    @app.get('/reorganization/approval', response_class=HTMLResponse)
    def proposals(request: Request):
        administration(request)
        try:
            names = sorted(p.name[:64] for p in Path(inbox).iterdir()
                           if p.name.endswith('.proposal.json') and len(p.name) == 78)
            for name in names:
                approval.identity(name)
        except (ValueError, OSError):
            raise HTTPException(503, 'Hostitelská schvalovací schránka není dostupná.')
        return templates.TemplateResponse(request, 'reorganization_proposals.html', {'sources': names},
                                          headers={'Cache-Control': 'no-store'})

    @app.get('/reorganization/approval/{source}', response_class=HTMLResponse)
    def proposal(request: Request, source: str):
        administration(request)
        return render(request, source)

    @app.get('/reorganization/approval/{source}/manifest')
    def download(request: Request, source: str):
        administration(request)
        _, original, final = load(source)
        return Response(manifest_to_json(final or original), media_type='application/json',
            headers={'Cache-Control': 'no-store', 'Content-Disposition': 'attachment; filename="execution-manifest.json"'})

    @app.post('/reorganization/approval/{source}/prepare', response_class=HTMLResponse)
    async def prepare(request: Request, source: str):
        administration(request)
        bundle, original, existing = await run_in_threadpool(load, source)
        fields = await form(request, source, approval.sha256(existing or original))
        if existing is not None:
            raise HTTPException(409, 'Review už existuje; nelze je přepsat ani znovu připravit.')
        actor = actor_value(fields.get('actor', [''])[0])
        try:
            final = await run_in_threadpool(approval.prepare_review, bundle, actor=actor,
                                           warning_ids=fields.get('warnings', []))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        result = await run_in_threadpool(fresh, source, final)
        try:
            await run_in_threadpool(approval.write_review, outbox, source, final)
        except FileExistsError:
            raise HTTPException(409, 'Review už existuje.')
        except (ValueError, OSError):
            raise HTTPException(503, 'STOP: review se nepodařilo bezpečně uložit; neopakovat automaticky.')
        return await run_in_threadpool(render, request, source, result=result, prepared_actor=actor)

    @app.post('/reorganization/approval/{source}/approve', response_class=HTMLResponse)
    async def approve(request: Request, source: str):
        administration(request)
        bundle, _original, final = await run_in_threadpool(load, source)
        if final is None:
            raise HTTPException(409, 'Nejprve připravte přesný canonical manifest k review.')
        fields = await form(request, source, approval.sha256(final))
        if fields.get('sha256') != [approval.sha256(final)] or fields.get('confirm') != ['yes']:
            raise HTTPException(422, 'Vyžadováno výslovné schválení zobrazeného SHA-256.')
        actor = actor_value(fields.get('actor', [''])[0])
        try:
            approval.validate_review(bundle, final, actor=actor)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        result = await run_in_threadpool(fresh, source, final)
        try:
            await run_in_threadpool(approval.write_approval, outbox, source, final,
                                    actor=actor, approved_sha256=fields['sha256'][0])
        except FileExistsError:
            raise HTTPException(409, 'Manifest byl již jednorázově schválen.')
        except (ValueError, OSError):
            raise HTTPException(503, 'STOP: schválení se nepodařilo bezpečně uložit; neopakovat automaticky.')
        return await run_in_threadpool(render, request, source, result=result)


def as_view(manifest):
    from dataclasses import asdict
    view = asdict(manifest)
    # Nanosecond evidence exceeds JavaScript's safe integer range. Only the
    # presentation data uses strings; the approved canonical JSON stays exact.
    for action in view['payload']['actions']:
        action['expected_mtime_ns'] = str(action['expected_mtime_ns'])
        action['expected_size'] = str(action['expected_size'])
    return view
