"""Read-only web adapter for the existing V6 planner, with request-scoped work."""
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Lock

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from .models import CatalogCollection, CatalogTitle, ExternalSubtitle, Video
from .target_execution_guard import database_path_from_url, ExecutionGuardStopped
from .target_planner_coverage import report_coverage
from .target_planner_filesystem import inventory_filesystem, plan_library
from .target_planner_service import load_planner_context, readonly_planner_session

logger = logging.getLogger(__name__)
STATUSES = ('BLOCKED', 'REVIEW', 'WARNING', 'READY', 'SYSTEM_EXCLUDED')


def database_path(database_url):
    url = make_url(database_url)
    # Reopen RO ourselves, accepting only the deployed SQLite URI options.
    if (url.query.keys() - {'uri', 'mode'} or url.query.get('mode', 'rw') not in {'ro', 'rw'}
            or (url.database or '').startswith('file:') and url.query.get('uri') != 'true'
            or any(c in (url.database or '') for c in ('\x00', '\\', '?', '#'))):
        raise ValueError('Unsupported SQLite preview URL.')
    try:
        path = database_path_from_url(url)
    except (ExecutionGuardStopped, OSError) as exc:
        raise ValueError('Unsupported SQLite preview URL.') from exc
    if path is None or '\x00' in str(path):
        raise ValueError('Preview requires a file-backed SQLite database.')
    return path


def library_counts(database):
    with readonly_planner_session(database) as session:
        return {label: session.scalar(select(func.count()).select_from(model))
                for label, model in (('Kolekce', CatalogCollection), ('Části', CatalogTitle),
                                     ('Videa', Video), ('Externí titulky', ExternalSubtitle))}


def build_preview(database, library_root, *, require_mount=False, access_timeout=10):
    """Worker-only: no app startup, migrations, scanner writes or hashing."""
    from .scanner import check_library_access, LibraryUnavailableError
    root = Path(library_root)
    check_library_access(root, timeout_seconds=access_timeout, require_mount=require_mount)
    with readonly_planner_session(Path(database)) as session:
        context = load_planner_context(session)
    snapshot = inventory_filesystem(root, hash_archives=False)
    if context.videos and not any(e.kind == 'regular' for e in snapshot.entries):
        raise LibraryUnavailableError('Knihovna je prázdná nebo nepřístupná proti neprázdné databázi.')
    plan = plan_library(context, snapshot)
    return {'plan': asdict(plan), 'coverage': asdict(report_coverage(snapshot, plan))}


def _stop_worker(process):
    # Include the access-check child. No unbounded communicate()/wait() after kill.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass


def run_preview(database, library_root, *, timeout, require_mount=False, access_timeout=10):
    process = subprocess.Popen(
        [sys.executable, '-B', '-m', 'app.tools.reorganization_preview',
         str(database), str(library_root), str(int(require_mount)), str(access_timeout)],
        cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException as exc:
        _stop_worker(process)
        if isinstance(exc, subprocess.TimeoutExpired):
            exc.worker = process  # Admission stays closed if kernel I/O outlives SIGKILL.
        raise
    if process.returncode:
        # ASCII escaping prevents control characters/invalid bytes entering logs.
        diagnostic = repr(stderr[-2048:])[1:-1]
        logger.warning('Reorganization preview worker failed (exit %s): %s', process.returncode, diagnostic)
        raise subprocess.CalledProcessError(process.returncode, 'reorganization-preview')
    return json.loads(stdout)


def display_safe(value):
    """Render undecodable filename bytes visibly, without changing planner evidence."""
    if isinstance(value, str):
        return value.encode('utf-8', 'backslashreplace').decode('utf-8')
    if isinstance(value, dict):
        return {display_safe(k): display_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [display_safe(v) for v in value]
    return value


def preview_presentation(preview):
    coverage, plan = preview['coverage'], preview['plan']
    counts = dict(coverage['counts'])
    if not coverage['inventory_complete']:
        state = 'INVENTORY_INCOMPLETE'
    elif not coverage['files_complete']:
        state = 'FILES_UNRESOLVED'
    elif not coverage['complete'] or any(r['warnings'] or r['status'] == 'WARNING' for r in plan['records']):
        state = 'DECISIONS_REQUIRED'
    elif not plan['records']:
        state = 'EMPTY'
    else:
        state = 'COVERED'
    return {'state': state, 'operations': {k: counts.get(k, 0) for k in ('MOVE', 'KEEP', 'QUARANTINE', 'REVIEW')},
            'new_directories': counts.get('directory_MISSING_TARGET_PARENT', 0)}


def install_reorganization_routes(app, templates, settings):
    lock = app.state.reorganization_preview_lock = Lock()
    app.state.reorganization_stopping_worker = None
    timeout = settings.reorganization_preview_timeout_seconds

    def render(request, *, preview=None, error=None, status_code=200):
        try:
            counts = library_counts(database_path(settings.database_url))
        except (OSError, ValueError, SQLAlchemyError):
            counts = None
            error = error or 'Databáze není dostupná pro read-only náhled.'
            status_code = 503 if status_code == 200 else status_code
        view = preview_presentation(preview) if preview is not None else None
        response = templates.TemplateResponse(request, 'reorganization.html', display_safe({
            'counts': counts, 'library_root': str(settings.anime_path),
            'preview': preview, 'view': view, 'error': error, 'statuses': STATUSES,
            'timeout': timeout, 'preference_return_to': '/reorganization',
            'approval_available': bool(settings.reorganization_approval_inbox),
        }), status_code=status_code)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/reorganization', response_class=HTMLResponse)
    def reorganization(request: Request):
        return render(request)

    @app.get('/reorganization/preview')
    def preview_redirect(request: Request):
        return RedirectResponse('/reorganization', status_code=303)

    @app.post('/reorganization/preview', response_class=HTMLResponse)
    def reorganization_preview(request: Request):
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return render(request, error='Náhled vyžaduje požadavek ze stejného webu.', status_code=403)
        if not lock.acquire(blocking=False):
            return render(request, error='Jiný náhled právě probíhá. Zkuste požadavek později.', status_code=409)
        try:
            previous = app.state.reorganization_stopping_worker
            if previous is not None and previous.poll() is None:
                return render(request, error='Předchozí výpočet se ještě ukončuje. Zkuste požadavek později.', status_code=409)
            app.state.reorganization_stopping_worker = None
            preview = run_preview(database_path(settings.database_url), settings.anime_path, timeout=timeout,
                                  require_mount=settings.require_mount, access_timeout=settings.library_access_timeout_seconds)
            return render(request, preview=preview)
        except subprocess.TimeoutExpired as exc:
            app.state.reorganization_stopping_worker = getattr(exc, 'worker', None)
            return render(request, error=f'Náhled překročil časový limit {timeout:g} s. '
                          'Výsledek není k dispozici; zkuste jej vytvořit znovu.', status_code=504)
        except (OSError, ValueError, SQLAlchemyError, subprocess.CalledProcessError):
            logger.warning('Read-only reorganization preview unavailable')
            return render(request, error='Náhled se nepodařilo vytvořit. Knihovna nebo databáze není dostupná '
                          'nebo není připojen požadovaný mount; žádný platný plán není k dispozici.', status_code=503)
        finally:
            lock.release()

    from .reorganization_approval_web import install_approval_routes
    install_approval_routes(app, templates, settings)
