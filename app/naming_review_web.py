"""Physical Naming routes; GET/preview are read-only and POST writes choices only."""
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .models import CatalogCollection, CatalogTitle
from .layout_review import build_layout_review, review_tabs
from .layout_review_service import load_layout_review_context
from .naming_review import build_naming_review, filter_naming_units, make_naming_candidate
from .naming_review_service import (
    StaleNamingForm, apply_naming_review_decision,
    validate_naming_fingerprint,
)

FILTERS = (('pending', 'K vyřízení'), ('roots', 'Root názvy'), ('titles', 'Názvy částí'), ('confirmed', 'Potvrzené'), ('all', 'Vše'))


def install_naming_review_routes(app, templates, sessions, safe_redirect_target, redirect_response):
    def render(request, index, *, status='pending', q='', collection_id=None, title_id=None,
               error=None, submitted=None, custom_preview=None, status_code=200, return_to=None, layout_index=None):
        try:
            rows = filter_naming_units(index, status=status, q=q, collection_id=collection_id, title_id=title_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        params = {'status': status, 'q': q}
        if collection_id is not None: params['collection_id'] = collection_id
        if title_id is not None: params['title_id'] = title_id
        # A rejected POST must show its owner even if the previous filter would
        # hide that row after a metadata/context change.
        if submitted:
            scope, owner_id = submitted['owner_tag'].split(':')
            owner_key = (scope, int(owner_id))
            if owner_key in index.units and not any(u.owner_key == owner_key for u in rows):
                rows = (*rows, index.units[owner_key])
        filter_links = []
        for key, label in FILTERS:
            count = len(filter_naming_units(index, status=key, q=q, collection_id=collection_id, title_id=title_id))
            filter_links.append((key, label, '/naming-review?' + urlencode({**params, 'status': key}), count))
        return templates.TemplateResponse(request, 'naming_review.html', {
            'rows': rows, 'status': status, 'q': q, 'collection_id': collection_id, 'title_id': title_id,
            # A rejected POST keeps the caller's queue for the corrected retry.
            'filter_links': filter_links, 'return_to': return_to or '/naming-review?' + urlencode(params),
            'error': error, 'message': None, 'submitted': submitted, 'custom_preview': custom_preview,
            'review_tabs': review_tabs(index, layout_index, active='names', collection_id=collection_id, title_id=title_id),
        }, status_code=status_code)

    @app.get('/naming-review', response_class=HTMLResponse)
    def naming_review(request: Request, status: str = 'pending', q: str = '', collection_id: int | None = None, title_id: int | None = None):
        with sessions() as session:
            context = load_layout_review_context(session)
            index = build_naming_review(context.naming)
            layout_index = build_layout_review(context)
        return render(request, index, status=status, q=q, collection_id=collection_id, title_id=title_id, layout_index=layout_index)

    async def checked_form(request):
        origin = request.headers.get('origin')
        if origin:
            try:
                supplied, current = urlsplit(origin), urlsplit(str(request.url))
                same_origin = (supplied.scheme, supplied.netloc) == (current.scheme, current.netloc)
            except ValueError:
                same_origin = False
            if not same_origin:
                raise HTTPException(403, 'Požadavek musí pocházet ze stejného webu.')
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, 'Požadavek musí pocházet ze stejného webu.')
        form = await request.form()
        allowed = {'fingerprint', 'candidate_key', 'custom_text', 'return_to'}
        if any(key not in allowed for key in form) or any(len(form.getlist(key)) != 1 for key in form):
            raise HTTPException(400, 'Neplatná pole formuláře pojmenování.')
        if any(not isinstance(value, str) for value in form.values()):
            raise HTTPException(400, 'Neplatný formulář pojmenování.')
        return form

    def owner_for(session, scope, owner_id):
        model = {'collection': CatalogCollection, 'title': CatalogTitle}.get(scope)
        if model is None: raise HTTPException(404, 'Neznámý naming scope.')
        owner = session.get(model, owner_id)
        if owner is None: raise HTTPException(404, 'Položka pojmenování nebyla nalezena.')
        return owner

    def owner_index(session, owner):
        cid = owner.id if isinstance(owner, CatalogCollection) else owner.catalog_collection_id
        context = load_layout_review_context(
            session, collection_ids=[cid] if cid is not None else [],
            title_ids=[owner.id] if isinstance(owner, CatalogTitle) else [],
        )
        return build_naming_review(context.naming), build_layout_review(context)

    @app.post('/naming-review/{scope}/{owner_id}/preview')
    async def naming_preview(request: Request, scope: str, owner_id: int):
        form = await checked_form(request)
        with sessions() as session:
            owner = owner_for(session, scope, owner_id)
            index, layout_index = owner_index(session, owner)
            unit = index.units[scope, owner.id]
            try: validate_naming_fingerprint(unit, form.get('fingerprint'))
            except StaleNamingForm as exc: return JSONResponse({'detail': str(exc)}, status_code=409)
            candidate = make_naming_candidate(index.context, unit.owner_key, form.get('custom_text', ''), 'custom')
        return JSONResponse({
            'preview_text': candidate.sanitized.preview_text, 'chars': candidate.sanitized.chars,
            'utf8_bytes': candidate.sanitized.utf8_bytes, 'max_component_bytes': candidate.max_component_bytes,
            'valid': candidate.can_confirm, 'transformations': candidate.transformation_labels,
            'diagnostics': candidate.diagnostic_messages,
        })

    async def decide(request, scope, owner_id, action):
        form = await checked_form(request)
        return_to = safe_redirect_target(str(form.get('return_to') or '/naming-review'))
        with sessions() as session:
            owner = owner_for(session, scope, owner_id)
            index, layout_index = owner_index(session, owner)
            submitted = {'owner_tag': f'{scope}:{owner.id}', 'candidate_key': form.get('candidate_key', ''), 'custom_text': form.get('custom_text', '')}
            try:
                apply_naming_review_decision(session, owner, index, action=action,
                    fingerprint=form.get('fingerprint'), candidate_key=form.get('candidate_key', ''), custom_text=form.get('custom_text', ''))
                session.commit()
            except ValueError as exc:
                session.rollback()
                preview = make_naming_candidate(index.context, (scope, owner.id), submitted['custom_text'], 'custom') if submitted['candidate_key'] == 'custom' else None
                return render(request, index, status='all', collection_id=index.units[scope, owner.id].collection_id,
                    error=str(exc), submitted=submitted, custom_preview=preview, status_code=409 if isinstance(exc, StaleNamingForm) else 400,
                    return_to=return_to, layout_index=layout_index)
        return redirect_response(return_to)

    @app.post('/naming-review/{scope}/{owner_id}/save')
    async def naming_save(request: Request, scope: str, owner_id: int):
        return await decide(request, scope, owner_id, 'save')

    @app.post('/naming-review/{scope}/{owner_id}/reset')
    async def naming_reset(request: Request, scope: str, owner_id: int):
        return await decide(request, scope, owner_id, 'reset')

    @app.post('/naming-review/{scope}/{owner_id}/reconfirm')
    async def naming_reconfirm(request: Request, scope: str, owner_id: int):
        return await decide(request, scope, owner_id, 'reconfirm')
