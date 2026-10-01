"""Read-only layout review and isolated, explicit human POST decisions."""
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from .layout_review import build_layout_review, filter_layout_units, review_tabs
from .layout_review_service import StaleLayoutForm, apply_layout_review_decision, load_layout_review_context
from .models import CatalogTitle
from .naming_review import build_naming_review


FILTERS = (('pending', 'K vyřízení'), ('own', 'Vlastní složka'), ('shared', 'Sdílené'), ('confirmed', 'Potvrzené'), ('all', 'Vše'))


def install_layout_review_routes(app, templates, sessions, safe_redirect_target, redirect_response):
    def render(request, index, *, status='pending', q='', collection_id=None, title_id=None,
               error=None, submitted=None, status_code=200, return_to=None):
        try:
            rows = filter_layout_units(index, status=status, q=q, collection_id=collection_id, title_id=title_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        params = {'status': status, 'q': q}
        if collection_id is not None: params['collection_id'] = collection_id
        if title_id is not None: params['title_id'] = title_id
        if submitted and submitted['title_id'] in index.units and not any(u.title_id == submitted['title_id'] for u in rows):
            rows = (*rows, index.units[submitted['title_id']])
        links = [(key, label, '/naming-review/layout?' + urlencode({**params, 'status': key}),
            len(filter_layout_units(index, status=key, q=q, collection_id=collection_id, title_id=title_id))) for key, label in FILTERS]
        tabs = review_tabs(build_naming_review(index.context.naming), index, active='layout', collection_id=collection_id, title_id=title_id)
        return templates.TemplateResponse(request, 'layout_review.html', {
            'rows': rows, 'status': status, 'q': q, 'collection_id': collection_id, 'title_id': title_id,
            'filter_links': links, 'review_tabs': tabs, 'error': error, 'submitted': submitted,
            'return_to': return_to or '/naming-review/layout?' + urlencode(params),
        }, status_code=status_code)

    @app.get('/naming-review/layout', response_class=HTMLResponse)
    def layout_review(request: Request, status: str = 'pending', q: str = '', collection_id: int | None = None, title_id: int | None = None):
        with sessions() as session:
            index = build_layout_review(load_layout_review_context(session))
        return render(request, index, status=status, q=q, collection_id=collection_id, title_id=title_id)

    async def decide(request, title_id, action):
        origin = request.headers.get('origin')
        if origin:
            try:
                supplied, current = urlsplit(origin), urlsplit(str(request.url))
                same_origin = (supplied.scheme, supplied.netloc) == (current.scheme, current.netloc)
            except ValueError:
                same_origin = False
            if not same_origin: raise HTTPException(403, 'Požadavek musí pocházet ze stejného webu.')
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, 'Požadavek musí pocházet ze stejného webu.')
        form = await request.form()
        if any(k not in {'fingerprint', 'candidate_key', 'return_to'} or len(form.getlist(k)) != 1 for k in form) or any(not isinstance(v, str) for v in form.values()):
            raise HTTPException(400, 'Neplatný formulář rozložení.')
        return_to = safe_redirect_target(form.get('return_to') or '/naming-review/layout')
        with sessions() as session:
            owner = session.get(CatalogTitle, title_id)
            if owner is None: raise HTTPException(404, 'Část neexistuje.')
            index = build_layout_review(load_layout_review_context(session,
                collection_ids=[owner.catalog_collection_id] if owner.catalog_collection_id is not None else [], title_ids=[owner.id]))
            try:
                apply_layout_review_decision(session, owner, index, action=action,
                    fingerprint=form.get('fingerprint'), candidate_key=form.get('candidate_key', ''))
                session.commit()
            except ValueError as exc:
                session.rollback()
                return render(request, index, status='all', collection_id=owner.catalog_collection_id,
                    error=str(exc), submitted={'title_id': title_id, 'candidate_key': form.get('candidate_key')},
                    status_code=409 if isinstance(exc, StaleLayoutForm) else 400, return_to=return_to)
        return redirect_response(return_to)

    @app.post('/naming-review/layout/title/{title_id}/save')
    async def layout_save(request: Request, title_id: int):
        return await decide(request, title_id, 'save')

    @app.post('/naming-review/layout/title/{title_id}/reconfirm')
    async def layout_reconfirm(request: Request, title_id: int):
        return await decide(request, title_id, 'reconfirm')

    @app.post('/naming-review/layout/title/{title_id}/reset')
    async def layout_reset(request: Request, title_id: int):
        return await decide(request, title_id, 'reset')
