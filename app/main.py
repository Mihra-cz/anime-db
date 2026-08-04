from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .config import Settings, get_settings
from .database import Base, make_engine, make_session_factory
from .models import Video
from .scanner import scan_library

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def _language_group(video: Video) -> set[str]:
    aliases = {"cs": "cs", "ces": "cs", "cze": "cs", "sk": "sk", "slk": "sk", "slo": "sk"}
    languages = {track.language for track in video.internal_subtitles}
    languages.update(track.language for track in video.external_subtitles)
    return {aliases.get(language.casefold(), language.casefold()) for language in languages}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        Base.metadata.create_all(engine)
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
    def index(request: Request, message: str | None = None, error: str | None = None):
        with sessions() as session:
            videos = session.scalars(select(Video).options(
                selectinload(Video.internal_subtitles), selectinload(Video.external_subtitles)
            )).all()
        folders: dict[str, dict[str, int]] = {}
        for video in videos:
            item = folders.setdefault(video.root_folder, {"total": 0, "cs": 0, "sk": 0, "missing": 0})
            item["total"] += 1
            languages = _language_group(video)
            item["cs"] += "cs" in languages
            item["sk"] += "sk" in languages
            item["missing"] += not ({"cs", "sk"} & languages)
        totals = {key: sum(folder[key] for folder in folders.values()) for key in ("total", "cs", "sk", "missing")}
        return templates.TemplateResponse(request, "index.html", {
            "folders": sorted(folders.items()), "totals": totals, "message": message, "error": error,
        })

    @app.get("/folders/{folder:path}", response_class=HTMLResponse)
    def folder_detail(request: Request, folder: str):
        with sessions() as session:
            videos = session.scalars(select(Video).where(Video.root_folder == folder).options(
                selectinload(Video.audio_tracks), selectinload(Video.internal_subtitles),
                selectinload(Video.external_subtitles)).order_by(Video.relative_path)).all()
        return templates.TemplateResponse(request, "folder.html", {
            "folder": folder, "videos": videos, "language_group": _language_group,
        })

    @app.post("/scan")
    def scan():
        try:
            with sessions() as session:
                result = scan_library(session, settings.anime_path)
            message = (f"Sken dokončen: {result.found} videí, {result.created} nových, "
                       f"{result.updated} změněných, {result.errors} chyb.")
            return RedirectResponse(url=f"/?message={message}", status_code=303)
        except Exception as exc:
            logger.exception("Sken selhal")
            return RedirectResponse(url=f"/?error={str(exc)}", status_code=303)

    return app


app = create_app()
