from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .catalog import (
    FILTER_LABELS,
    build_catalog_results,
    derive_episode_number,
    derive_season_info,
    determine_parent_series,
    group_videos_by_series,
    normalize_search_query,
    set_manual_hardsub,
    sort_title_videos,
    title_videos,
    translation_status,
    video_matches_filter,
)
from .config import Settings, get_settings
from .database import Base, make_engine, make_session_factory
from .migrations import migrate_schema
from .metadata.providers.anilist import AniListProvider
from .metadata.providers.base import MetadataProviderError
from .models import CatalogTitle, Video
from .scanner import LibrarySafetyError, scan_library

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
METADATA_STATUS_LABELS = {
    "unlinked": "Bez metadat", "candidates_available": "Čeká na potvrzení",
    "linked_auto": "Spárováno automaticky", "linked_manual": "Spárováno ručně",
    "conflict": "Konflikt", "unavailable": "Bez externího záznamu", "error": "Chyba",
}
def _load_videos(sessions) -> list[Video]:
    with sessions() as session:
        return list(session.scalars(select(Video).options(
            selectinload(Video.audio_tracks), selectinload(Video.internal_subtitles),
            selectinload(Video.external_subtitles), selectinload(Video.catalog_title),
        ).order_by(Video.relative_path)).all())


def hardsub_return_url(
    filter_name: str, series_path: str | int, video_id: int, query: str = "",
    sort: str = "", direction: str = "", video_sort: str = "",
    video_direction: str = "",
) -> str:
    parameters = {"catalog_title_id": series_path} if isinstance(series_path, int) else {"series_path": series_path}
    if normalized_query := normalize_search_query(query):
        parameters["q"] = normalized_query
    if sort:
        parameters.update(sort=sort, direction=direction)
    if video_sort:
        parameters.update(video_sort=video_sort, video_direction=video_direction)
    query = urlencode(parameters)
    return f"/catalog/{filter_name}/series?{query}#video-{video_id}"


def catalog_state_url(
    filter_name: str, query: str = "", sort: str = "", direction: str = ""
) -> str:
    parameters = {}
    if query:
        parameters["q"] = query
    if sort:
        parameters.update(sort=sort, direction=direction)
    return f"/catalog/{filter_name}" + (f"?{urlencode(parameters)}" if parameters else "")


def series_state_url(
    filter_name: str, series_path: str | int, query: str, sort: str, direction: str,
    video_sort: str = "", video_direction: str = "",
) -> str:
    parameters = {"catalog_title_id": series_path} if isinstance(series_path, int) else {"series_path": series_path}
    if query:
        parameters["q"] = query
    parameters.update(sort=sort, direction=direction)
    if video_sort:
        parameters.update(video_sort=video_sort, video_direction=video_direction)
    return f"/catalog/{filter_name}/series?{urlencode(parameters)}"


def toggled_direction(column: str, active_sort: str, active_direction: str) -> str:
    return "desc" if column == active_sort and active_direction == "asc" else "asc"


def _empty_stats() -> dict[str, int]:
    return {key: 0 for key in (
        "total", "episodes", "bonus", "cs", "sk", "only_cs", "only_sk", "both_cs_sk",
        "translated", "missing", "unknown",
    )}


def _add_video(stats: dict[str, int], video: Video) -> None:
    status = translation_status(video)
    stats["total"] += 1
    stats["episodes" if video.file_type == "episode" else "bonus"] += 1
    stats["cs"] += status.has_cs
    stats["sk"] += status.has_sk
    stats["only_cs"] += status.has_cs and not status.has_sk
    stats["only_sk"] += status.has_sk and not status.has_cs
    stats["both_cs_sk"] += status.has_cs and status.has_sk
    stats["translated"] += status.has_cs_or_sk
    stats["missing"] += not status.has_cs_or_sk
    stats["unknown"] += status.has_unknown


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        Base.metadata.create_all(engine)
        migrate_schema(engine)
        logger.info("AnimeDB spuštěno; knihovna=%s", settings.anime_path)
        yield
        engine.dispose()

    app = FastAPI(title="AnimeDB", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.metadata_provider = AniListProvider(settings.metadata_request_timeout_seconds)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        message: str | None = None,
        error: str | None = None,
        confirm_deletions: bool = False,
        q: str = "",
    ):
        with sessions() as session:
            videos = session.scalars(select(Video).options(
                selectinload(Video.internal_subtitles), selectinload(Video.external_subtitles)
            )).all()
        folders: dict[str, dict[str, int]] = {}
        totals = _empty_stats()
        for video in videos:
            _add_video(folders.setdefault(video.root_folder, _empty_stats()), video)
            _add_video(totals, video)
        return templates.TemplateResponse(request, "index.html", {
            "folders": sorted(folders.items()), "totals": totals, "message": message,
            "error": error, "confirm_deletions": confirm_deletions,
            "q": normalize_search_query(q),
        })

    @app.get("/folders/{folder:path}", response_class=HTMLResponse)
    def folder_detail(request: Request, folder: str):
        videos = [video for video in _load_videos(sessions) if video.root_folder == folder]
        results = build_catalog_results(videos, "all")
        def sort_url(column: str) -> str:
            return catalog_state_url(
                "all", "", column,
                toggled_direction(column, results.sort, results.direction),
            )
        return templates.TemplateResponse(request, "catalog.html", {
            "filter_name": "all", "filter_label": f"Složka: {folder}",
            "groups": results.groups, "video_count": results.video_count, "q": "",
            "all_filters": FILTER_LABELS, "sort": results.sort,
            "direction": results.direction, "sort_url": sort_url,
            "catalog_state_url": catalog_state_url,
        })

    @app.get("/catalog/{filter_name}", response_class=HTMLResponse)
    def catalog(
        request: Request, filter_name: str, q: str = "",
        sort: str | None = None, direction: str | None = None,
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        videos = _load_videos(sessions)
        results = build_catalog_results(videos, filter_name, q, sort, direction)
        def sort_url(column: str) -> str:
            return catalog_state_url(
                filter_name, results.query, column,
                toggled_direction(column, results.sort, results.direction),
            )
        return templates.TemplateResponse(request, "catalog.html", {
            "filter_name": filter_name,
            "filter_label": FILTER_LABELS[filter_name],
            "groups": results.groups,
            "video_count": results.video_count,
            "q": results.query,
            "sort": results.sort, "direction": results.direction,
            "sort_url": sort_url, "catalog_state_url": catalog_state_url,
            "all_filters": FILTER_LABELS,
        })

    @app.get("/catalog/{filter_name}/series", response_class=HTMLResponse)
    def series_detail(
        request: Request, filter_name: str, catalog_title_id: int | None = None,
        series_path: str | None = None, q: str = "",
        sort: str | None = None, direction: str | None = None,
        video_sort: str | None = None, video_direction: str | None = None,
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        results = build_catalog_results(
            _load_videos(sessions), filter_name, q, sort, direction
        )
        with sessions() as session:
            catalog_title = session.get(CatalogTitle, catalog_title_id) if catalog_title_id else None
            if catalog_title is None and series_path:
                catalog_title = session.scalar(select(CatalogTitle).where(
                    CatalogTitle.relative_root_path == series_path
                ))
        selected_path = catalog_title.relative_root_path if catalog_title else series_path
        videos, normalized_video_sort, normalized_video_direction = sort_title_videos(
            results.videos_by_title.get(selected_path or "", []), video_sort, video_direction
        )
        if not videos:
            raise HTTPException(status_code=404, detail="Série nebyla nalezena")
        def video_sort_url(column: str) -> str:
            return series_state_url(
                filter_name, catalog_title.id if catalog_title else (series_path or selected_path or ""),
                results.query, results.sort, results.direction,
                column, toggled_direction(column, normalized_video_sort, normalized_video_direction),
            )
        return templates.TemplateResponse(request, "series.html", {
            "filter_name": filter_name,
            "filter_label": FILTER_LABELS[filter_name],
            "series": determine_parent_series(videos[0].relative_path),
            "catalog_title": catalog_title,
            "metadata_status_labels": METADATA_STATUS_LABELS,
            "metadata_candidates": [], "metadata_error": None,
            "videos": videos,
            "translation_status": translation_status,
            "video_matches_filter": video_matches_filter,
            "derive_season_info": derive_season_info,
            "derive_episode_number": derive_episode_number,
            "q": results.query,
            "sort": results.sort, "direction": results.direction,
            "video_sort": normalized_video_sort,
            "video_direction": normalized_video_direction,
            "video_sort_url": video_sort_url,
            "back_url": catalog_state_url(
                filter_name, results.query, results.sort, results.direction
            ),
        })

    @app.post("/catalog/{filter_name}/titles/{catalog_title_id}/metadata/search", response_class=HTMLResponse)
    def search_metadata(
        request: Request, filter_name: str, catalog_title_id: int,
        metadata_query: str = Form(...), q: str = Form(""), sort: str = Form(""),
        direction: str = Form(""), video_sort: str = Form(""),
        video_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        with sessions() as session:
            catalog_title = session.get(CatalogTitle, catalog_title_id)
        if catalog_title is None:
            raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
        candidates, metadata_error = [], None
        if not settings.metadata_enabled or not settings.anilist_enabled:
            metadata_error = "Vyhledávání metadat je vypnuté v konfiguraci."
        else:
            try:
                candidates = app.state.metadata_provider.search_titles(metadata_query)
            except (ValueError, MetadataProviderError) as exc:
                metadata_error = str(exc)
        all_videos = _load_videos(sessions)
        results = build_catalog_results(all_videos, filter_name, q, sort, direction)
        videos, normalized_video_sort, normalized_video_direction = sort_title_videos(
            results.videos_by_title.get(catalog_title.relative_root_path, []),
            video_sort, video_direction,
        )
        if not videos:
            raise HTTPException(status_code=404, detail="Titul neodpovídá aktivnímu filtru")
        def video_sort_url(column: str) -> str:
            return series_state_url(
                filter_name, catalog_title.id, results.query, results.sort, results.direction,
                column, toggled_direction(column, normalized_video_sort, normalized_video_direction),
            )
        return templates.TemplateResponse(request, "series.html", {
            "filter_name": filter_name, "filter_label": FILTER_LABELS[filter_name],
            "series": determine_parent_series(videos[0].relative_path),
            "catalog_title": catalog_title, "videos": videos,
            "translation_status": translation_status, "video_matches_filter": video_matches_filter,
            "derive_season_info": derive_season_info, "derive_episode_number": derive_episode_number,
            "q": results.query, "sort": results.sort, "direction": results.direction,
            "video_sort": normalized_video_sort, "video_direction": normalized_video_direction,
            "video_sort_url": video_sort_url,
            "back_url": catalog_state_url(filter_name, results.query, results.sort, results.direction),
            "metadata_status_labels": METADATA_STATUS_LABELS,
            "metadata_candidates": candidates, "metadata_error": metadata_error,
            "metadata_query": metadata_query[:200],
        })

    @app.post("/videos/{video_id}/hardsub")
    def update_hardsub(
        video_id: int,
        mode: str = Form(...),
        filter_name: str = Form(...),
        series_path: str = Form(""),
        catalog_title_id: int | None = Form(None),
        q: str = Form(""),
        sort: str = Form(""),
        direction: str = Form(""),
        video_sort: str = Form(""),
        video_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=400, detail="Neplatný návratový filtr")
        with sessions() as session:
            video = session.get(Video, video_id)
            if video is None:
                raise HTTPException(status_code=404, detail="Video nebylo nalezeno")
            try:
                set_manual_hardsub(video, mode)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            session.commit()
        if catalog_title_id or series_path:
            target = hardsub_return_url(
                filter_name, catalog_title_id or series_path, video_id, q, sort, direction,
                video_sort, video_direction,
            )
        else:
            target = f"/catalog/{filter_name}"
        return RedirectResponse(target, status_code=303)

    @app.post("/scan")
    def scan(confirm_deletions: bool = Form(False)):
        try:
            with sessions() as session:
                result = scan_library(
                    session,
                    settings.anime_path,
                    require_mount=settings.require_mount,
                    confirm_deletions=confirm_deletions,
                )
            message = (f"Sken dokončen: {result.found} videí, {result.created} nových, "
                       f"{result.updated} změněných, {result.errors} chyb.")
            return RedirectResponse(url=f"/?{urlencode({'message': message})}", status_code=303)
        except LibrarySafetyError as exc:
            logger.warning("Sken bezpečnostně přerušen: %s", exc)
            query = {"error": str(exc)}
            if exc.confirmation_allowed:
                query["confirm_deletions"] = "true"
            return RedirectResponse(url=f"/?{urlencode(query)}", status_code=303)
        except Exception as exc:
            logger.exception("Sken selhal")
            message = f"Sken selhal. Knihovna může být odpojená: {exc}"
            return RedirectResponse(url=f"/?{urlencode({'error': message})}", status_code=303)

    return app


app = create_app()
