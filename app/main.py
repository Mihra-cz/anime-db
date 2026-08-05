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
    determine_parent_series,
    group_videos_by_series,
    set_manual_hardsub,
    translation_status,
)
from .config import Settings, get_settings
from .database import Base, make_engine, make_session_factory
from .migrations import migrate_schema
from .models import Video
from .scanner import LibrarySafetyError, scan_library

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
FILTER_LABELS = {
    "only-cs": "Pouze CZ",
    "only-sk": "Pouze SK",
    "both": "CZ i SK",
    "missing": "Bez CZ/SK",
    "unknown": "Neznámé titulky",
}


def _matches_filter(video: Video, filter_name: str) -> bool:
    status = translation_status(video)
    return {
        "only-cs": status.has_cs and not status.has_sk,
        "only-sk": status.has_sk and not status.has_cs,
        "both": status.has_cs and status.has_sk,
        "missing": not status.has_cs_or_sk,
        "unknown": status.has_unknown,
    }[filter_name]


def _load_videos(sessions) -> list[Video]:
    with sessions() as session:
        return list(session.scalars(select(Video).options(
            selectinload(Video.internal_subtitles), selectinload(Video.external_subtitles)
        ).order_by(Video.relative_path)).all())


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
        })

    @app.get("/folders/{folder:path}", response_class=HTMLResponse)
    def folder_detail(request: Request, folder: str):
        with sessions() as session:
            videos = session.scalars(select(Video).where(Video.root_folder == folder).options(
                selectinload(Video.audio_tracks), selectinload(Video.internal_subtitles),
                selectinload(Video.external_subtitles)).order_by(Video.relative_path)).all()
        return templates.TemplateResponse(request, "folder.html", {
            "folder": folder, "videos": videos, "translation_status": translation_status,
        })

    @app.get("/catalog/{filter_name}", response_class=HTMLResponse)
    def catalog(request: Request, filter_name: str):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        videos = _load_videos(sessions)
        grouped = filter_name in {"missing", "unknown"}
        groups = group_videos_by_series(
            videos, lambda video: _matches_filter(video, filter_name)
        ) if grouped else []
        filtered_videos = [] if grouped else [
            video for video in videos if _matches_filter(video, filter_name)
        ]
        return templates.TemplateResponse(request, "catalog.html", {
            "filter_name": filter_name,
            "filter_label": FILTER_LABELS[filter_name],
            "grouped": grouped,
            "groups": groups,
            "videos": filtered_videos,
            "translation_status": translation_status,
        })

    @app.get("/catalog/{filter_name}/series", response_class=HTMLResponse)
    def series_detail(request: Request, filter_name: str, series_path: str):
        if filter_name not in {"missing", "unknown"}:
            raise HTTPException(status_code=404, detail="Neznámý seskupený filtr")
        videos = [
            video for video in _load_videos(sessions)
            if determine_parent_series(video.relative_path).relative_path == series_path
        ]
        if not videos:
            raise HTTPException(status_code=404, detail="Série nebyla nalezena")
        return templates.TemplateResponse(request, "series.html", {
            "filter_name": filter_name,
            "filter_label": FILTER_LABELS[filter_name],
            "series": determine_parent_series(videos[0].relative_path),
            "videos": videos,
            "translation_status": translation_status,
        })

    @app.post("/videos/{video_id}/hardsub")
    def update_hardsub(
        video_id: int,
        mode: str = Form(...),
        filter_name: str = Form(...),
        series_path: str = Form(""),
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
        if series_path:
            query = urlencode({"series_path": series_path})
            target = f"/catalog/{filter_name}/series?{query}"
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
