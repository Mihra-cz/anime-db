from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import time
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
    video_matches_search,
)
from .config import Settings, get_settings
from .database import Base, make_engine, make_session_factory
from .migrations import migrate_schema
from .hierarchy_review import (
    SIMPLE_DEFINITION_FIELDS, apply_manual_split, apply_single_season_suggestion,
    definitions_as_json, definitions_to_json, parse_manual_definitions,
    parse_simple_definitions,
    preview_assignments, simple_definition_rows, single_season_suggestion,
)
from .metadata.providers.anilist import AniListProvider
from .metadata.providers.base import MetadataProviderError
from .metadata.artwork import ArtworkCacheError, cache_cover
from .metadata.candidates import (
    LOW_SCORE_THRESHOLD, batch_search_candidates, decode_match_reasons, search_and_store_candidates,
    set_candidate_rejected,
)
from .metadata.service import (
    MetadataConflictError, MetadataLockedError, confirm_anilist_candidate,
    default_metadata_search_query, normalize_metadata_search_query, refresh_title_metadata,
    set_manual_display_title, unlink_title_metadata,
)
from .models import CatalogCollection, CatalogTitle, ExternalTitleLink, TitleMetadata, Video, utc_now
from .numbering import (
    apply_sequential_numbering, collection_requires_numbering_review,
    preview_sequential_numbering,
    recalculate_title_numbering, set_title_numbering, set_video_episode_override,
    summarize_title_numbering,
)
from .scanner import LibrarySafetyError, scan_library

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
METADATA_STATUS_LABELS = {
    "unlinked": "Bez metadat", "candidates_available": "Čeká na potvrzení",
    "linked_auto": "Spárováno automaticky", "linked_manual": "Spárováno ručně",
    "conflict": "Konflikt", "migration_review_required": "Vyžaduje kontrolu migrace",
    "unavailable": "Bez externího záznamu", "error": "Chyba",
}
def _load_videos(sessions) -> list[Video]:
    with sessions() as session:
        return list(session.scalars(select(Video).options(
            selectinload(Video.audio_tracks), selectinload(Video.internal_subtitles),
            selectinload(Video.external_subtitles),
            selectinload(Video.catalog_title).selectinload(CatalogTitle.collection),
            selectinload(Video.catalog_collection),
        ).order_by(Video.relative_path)).all())


def _load_catalog_title(session, catalog_title_id: int | None):
    if catalog_title_id is None:
        return None
    return session.scalar(select(CatalogTitle).options(
        selectinload(CatalogTitle.external_links),
        selectinload(CatalogTitle.metadata_record),
        selectinload(CatalogTitle.collection),
        selectinload(CatalogTitle.metadata_candidates),
        selectinload(CatalogTitle.artwork),
    ).where(CatalogTitle.id == catalog_title_id))


def _metadata_template_values(title: CatalogTitle | None, allow_remote_images: bool, show_rejected: bool = False) -> dict:
    metadata = title.metadata_record if title else None
    def decoded(value: str | None) -> list:
        try:
            result = json.loads(value or "[]")
            return result if isinstance(result, list) else []
        except (TypeError, ValueError):
            return []
    artwork = next((item for item in (title.artwork if title else []) if item.is_primary and item.artwork_type == "cover"), None)
    candidates = sorted(
        [item for item in (title.metadata_candidates if title else []) if show_rejected or item.rejected_at is None],
        key=lambda item: (item.rejected_at is not None, -(item.match_score or 0), item.candidate_title.casefold()),
    )
    return {
        "title_metadata": metadata,
        "external_links": sorted(
            title.external_links if title else [],
            key=lambda link: (not link.is_primary, link.provider, link.external_id),
        ),
        "metadata_genres": decoded(metadata.genres_json if metadata else None),
        "metadata_tags": decoded(metadata.tags_json if metadata else None),
        "metadata_synonyms": decoded(metadata.synonyms_json if metadata else None),
        "metadata_candidates": candidates,
        "candidate_reasons": {item.id: decode_match_reasons(item) for item in candidates},
        "low_score_threshold": LOW_SCORE_THRESHOLD,
        "show_rejected": show_rejected,
        "has_rejected_candidates": bool(title and any(item.rejected_at for item in title.metadata_candidates)),
        "local_cover_url": f"/artwork/{artwork.thumbnail_path}" if artwork and artwork.thumbnail_path else None,
        "show_remote_cover": bool(not artwork and allow_remote_images and metadata and metadata.cover_image_url),
    }


def hardsub_return_url(
    filter_name: str, series_path: str | int, video_id: int, query: str = "",
    sort: str = "", direction: str = "", video_sort: str = "",
    video_direction: str = "",
) -> str:
    parameters = {"filter_name": filter_name} if isinstance(series_path, int) else {"series_path": series_path}
    if normalized_query := normalize_search_query(query):
        parameters["q"] = normalized_query
    if sort:
        parameters.update(sort=sort, direction=direction)
    if video_sort:
        parameters.update(video_sort=video_sort, video_direction=video_direction)
    query = urlencode(parameters)
    base = f"/titles/{series_path}" if isinstance(series_path, int) else f"/catalog/{filter_name}/series"
    return f"{base}?{query}#video-{video_id}"


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


def metadata_return_url(
    filter_name: str, catalog_title_id: int, q: str = "", sort: str = "",
    direction: str = "", detail_sort: str = "", detail_direction: str = "",
    **messages: str,
) -> str:
    parameters: dict[str, str | int] = {"filter_name": filter_name}
    if q:
        parameters["q"] = normalize_search_query(q)
    if sort:
        parameters.update(sort=sort, direction=direction)
    if detail_sort:
        parameters.update(video_sort=detail_sort, video_direction=detail_direction)
    parameters.update({key: value for key, value in messages.items() if value})
    return f"/titles/{catalog_title_id}?{urlencode(parameters)}#metadata"


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
    settings.metadata_artwork_directory.mkdir(parents=True, exist_ok=True)
    app.mount("/artwork", StaticFiles(directory=settings.metadata_artwork_directory, check_dir=False), name="artwork")

    def cache_title_artwork(catalog_title_id: int, *, force: bool = False) -> str | None:
        if not settings.metadata_download_artwork:
            return None
        with sessions() as session:
            metadata = session.get(TitleMetadata, catalog_title_id)
            if not metadata or metadata.metadata_provider != "anilist" or not metadata.metadata_external_id or not metadata.cover_image_url:
                return "Metadata neobsahují URL obalu."
            try:
                cache_cover(
                    session, catalog_title_id=catalog_title_id,
                    provider=metadata.metadata_provider, external_id=metadata.metadata_external_id,
                    remote_url=metadata.cover_image_url, root=settings.metadata_artwork_directory,
                    max_bytes=settings.metadata_artwork_max_bytes,
                    thumbnail_width=settings.metadata_artwork_thumbnail_width,
                    timeout_seconds=settings.metadata_request_timeout_seconds, force=force,
                )
                session.commit()
            except ArtworkCacheError as exc:
                session.rollback()
                return str(exc)
        return None

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
        message: str | None = None, metadata_error: str | None = None,
        metadata_warning: str | None = None, show_rejected: bool = False,
        pending_external_id: str | None = None,
        require_conflict_confirmation: bool = False,
        require_locked_confirmation: bool = False,
        sequence_start: int | None = None, numbering_error: str | None = None,
        numbering_message: str | None = None,
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        all_videos = _load_videos(sessions)
        results = build_catalog_results(all_videos, filter_name, q, sort, direction)
        with sessions() as session:
            catalog_title = _load_catalog_title(session, catalog_title_id)
            if catalog_title is None and series_path:
                catalog_title = session.scalar(select(CatalogTitle).options(
                    selectinload(CatalogTitle.external_links),
                    selectinload(CatalogTitle.metadata_record),
                ).where(
                    CatalogTitle.relative_root_path == series_path
                ))
                if catalog_title is None:
                    legacy_collection = session.scalar(select(CatalogCollection).options(
                        selectinload(CatalogCollection.titles),
                    ).where(CatalogCollection.relative_root_path == series_path))
                    if legacy_collection and len(legacy_collection.titles) == 1:
                        catalog_title = _load_catalog_title(
                            session, legacy_collection.titles[0].id
                        )
        if catalog_title and request.url.path.startswith("/catalog/"):
            parameters = {"filter_name": filter_name}
            if q:
                parameters["q"] = normalize_search_query(q)
            if sort:
                parameters.update(sort=sort, direction=direction or "asc")
            if video_sort:
                parameters.update(
                    video_sort=video_sort, video_direction=video_direction or "asc"
                )
            return RedirectResponse(
                f"/titles/{catalog_title.id}?{urlencode(parameters)}", status_code=307
            )
        selected_path = catalog_title.relative_root_path if catalog_title else series_path
        title_candidates = [
            video for video in all_videos
            if (
                video.catalog_title_id == catalog_title.id
                if catalog_title else video.catalog_title
                and video.catalog_title.relative_root_path == selected_path
            )
        ]
        filtered_candidates = [
            video for video in title_candidates if video_matches_filter(video, filter_name)
        ]
        folded_query = results.query.casefold()
        title_query_match = bool(folded_query) and catalog_title and (
            folded_query in catalog_title.local_title.casefold()
            or folded_query in catalog_title.relative_root_path.casefold()
            or catalog_title.collection
            and folded_query in catalog_title.collection.local_title.casefold()
        )
        detail_videos = (
            filtered_candidates
            if not folded_query or title_query_match
            else [video for video in filtered_candidates if video_matches_search(video, folded_query)]
        )
        videos, normalized_video_sort, normalized_video_direction = sort_title_videos(
            detail_videos, video_sort, video_direction
        )
        if not videos:
            raise HTTPException(status_code=404, detail="Série nebyla nalezena")
        def video_sort_url(column: str) -> str:
            return series_state_url(
                filter_name, catalog_title.id if catalog_title else (series_path or selected_path or ""),
                results.query, results.sort, results.direction,
                column, toggled_direction(column, normalized_video_sort, normalized_video_direction),
            )
        numbering_preview = None
        if catalog_title and sequence_start is not None:
            try:
                numbering_preview = preview_sequential_numbering(
                    title_candidates, sequence_start
                )
            except ValueError as exc:
                numbering_error = str(exc)
        context = {
            "filter_name": filter_name,
            "filter_label": FILTER_LABELS[filter_name],
            "series": determine_parent_series(videos[0].relative_path),
            "catalog_title": catalog_title,
            "metadata_status_labels": METADATA_STATUS_LABELS,
            "metadata_candidates": [], "metadata_error": metadata_error,
            "metadata_message": message,
            "metadata_warning": metadata_warning,
            "numbering_error": numbering_error,
            "numbering_message": numbering_message,
            "numbering_preview": numbering_preview,
            "sequence_start": sequence_start,
            "pending_external_id": pending_external_id,
            "require_conflict_confirmation": require_conflict_confirmation,
            "require_locked_confirmation": require_locked_confirmation,
            "metadata_allow_remote_images": settings.metadata_allow_remote_images,
            "metadata_default_query": default_metadata_search_query(
                catalog_title
            ) if catalog_title else "",
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
            "back_url": (
                f"/collections/{catalog_title.catalog_collection_id}?"
                f"{urlencode({'filter_name': filter_name, 'q': results.query, 'sort': results.sort, 'direction': results.direction})}"
                if catalog_title and catalog_title.catalog_collection_id
                else catalog_state_url(filter_name, results.query, results.sort, results.direction)
            ),
        }
        context.update(_metadata_template_values(
            catalog_title, settings.metadata_allow_remote_images, show_rejected
        ))
        return templates.TemplateResponse(request, "series.html", context)

    @app.get("/titles/{catalog_title_id}", response_class=HTMLResponse)
    def title_detail(
        request: Request, catalog_title_id: int, filter_name: str = "all", q: str = "",
        sort: str | None = None, direction: str | None = None,
        video_sort: str | None = None, video_direction: str | None = None,
        message: str | None = None, metadata_error: str | None = None,
        metadata_warning: str | None = None, show_rejected: bool = False,
        pending_external_id: str | None = None,
        require_conflict_confirmation: bool = False,
        require_locked_confirmation: bool = False,
        sequence_start: int | None = None, numbering_error: str | None = None,
        numbering_message: str | None = None,
    ):
        return series_detail(
            request, filter_name, catalog_title_id, None, q, sort, direction,
            video_sort, video_direction, message, metadata_error, metadata_warning, show_rejected, pending_external_id,
            require_conflict_confirmation, require_locked_confirmation,
            sequence_start, numbering_error, numbering_message,
        )

    @app.get("/collections/{collection_id}", response_class=HTMLResponse)
    def collection_detail(
        request: Request, collection_id: int, filter_name: str = "all", q: str = "",
        sort: str | None = None, direction: str | None = None,
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        with sessions() as session:
            collection = session.scalar(select(CatalogCollection).options(
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_record),
            ).where(CatalogCollection.id == collection_id))
        if collection is None:
            raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
        all_videos = _load_videos(sessions)
        videos_by_part: dict[int, list[Video]] = {}
        for video in all_videos:
            if video.catalog_title and video.catalog_title.catalog_collection_id == collection.id:
                videos_by_part.setdefault(video.catalog_title_id, []).append(video)
        parts = []
        folded_query = normalize_search_query(q).casefold()
        collection_query_match = bool(folded_query) and (
            folded_query in collection.local_title.casefold()
            or folded_query in collection.relative_root_path.casefold()
        )
        for title in sorted(
            collection.titles,
            key=lambda value: (
                value.effective_sort_order,
                value.effective_season_number or 0,
                value.local_title.casefold(),
            ),
        ):
            title_videos_list = videos_by_part.get(title.id, [])
            stats = _empty_stats()
            for video in title_videos_list:
                _add_video(stats, video)
            filtered = [
                video for video in title_videos_list if video_matches_filter(video, filter_name)
            ]
            title_query_match = bool(folded_query) and (
                folded_query in title.local_title.casefold()
                or folded_query in title.relative_root_path.casefold()
            )
            matched = (
                filtered if not folded_query or collection_query_match or title_query_match
                else [video for video in filtered if video_matches_search(video, folded_query)]
            )
            if matched or title.metadata_status == "migration_review_required":
                parts.append({"title": title, "stats": stats, "metadata": title.metadata_record})
        state = {"filter_name": filter_name, "q": normalize_search_query(q)}
        if sort:
            state.update(sort=sort, direction=direction or "asc")
        return templates.TemplateResponse(request, "collection.html", {
            "collection": collection, "parts": parts, "filter_name": filter_name,
            "filter_label": FILTER_LABELS[filter_name], "q": normalize_search_query(q),
            "sort": sort or "", "direction": direction or "",
            "title_state_query": urlencode(state),
            "back_url": catalog_state_url(filter_name, q, sort or "", direction or ""),
            "metadata_status_labels": METADATA_STATUS_LABELS,
        })

    def hierarchy_review_context(
        request: Request, collection_id: int, definitions_json: str | None = None,
        preview=None, error: str | None = None, external_search_candidates=None,
        simple_rows=None, message: str | None = None,
    ):
        with sessions() as session:
            collection = session.scalar(select(CatalogCollection).options(
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_record),
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.external_links),
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
                selectinload(CatalogCollection.videos),
            ).where(CatalogCollection.id == collection_id))
            if collection is None:
                raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
            videos = sorted(collection.videos, key=lambda video: video.relative_path)
            definitions_json = definitions_json or definitions_as_json(collection)
            preview_rows = []
            if preview is not None:
                definitions = parse_manual_definitions(definitions_json)
                for video in videos:
                    indexes = preview.conflicts.get(video.id)
                    target = preview.assignments.get(video.id)
                    labels = (
                        [definitions[index].local_title for index in indexes]
                        if indexes else [definitions[target].local_title]
                        if target is not None else []
                    )
                    preview_rows.append({
                        "video": video, "targets": labels,
                        "conflict": indexes is not None,
                    })
            episode_numbers = [
                number for video in videos
                if (number := video.local_episode_number or derive_episode_number(video.filename))
                is not None
            ]
            external_candidates = [
                {"title": title, "metadata": title.metadata_record, "links": title.external_links}
                for title in collection.titles
                if title.metadata_record or title.external_links
            ]
            title_numbering = [
                {
                    "title": title,
                    "summary": summarize_title_numbering(
                        [video for video in videos if video.catalog_title_id == title.id]
                    ),
                }
                for title in sorted(
                    collection.titles,
                    key=lambda value: (
                        value.effective_sort_order,
                        value.local_title.casefold(),
                    ),
                )
            ]
            numbering_unknown = sum(
                item["summary"].unknown for item in title_numbering
            )
            season_one = single_season_suggestion(collection)
            return templates.TemplateResponse(request, "hierarchy_review_detail.html", {
                "collection": collection, "videos": videos,
                "episode_min": min(episode_numbers) if episode_numbers else None,
                "episode_max": max(episode_numbers) if episode_numbers else None,
                "definitions_json": definitions_json, "preview": preview,
                "preview_rows": preview_rows, "error": error,
                "external_candidates": external_candidates,
                "external_search_candidates": external_search_candidates or [],
                "metadata_status_labels": METADATA_STATUS_LABELS,
                "title_numbering": title_numbering,
                "numbering_unknown": numbering_unknown,
                "season_one_suggestion": season_one,
                "simple_rows": simple_rows or simple_definition_rows(collection),
                "message": message,
            })

    @app.get("/hierarchy-review", response_class=HTMLResponse)
    def hierarchy_review_list(request: Request):
        with sessions() as session:
            collections = list(session.scalars(select(CatalogCollection).options(
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
                selectinload(CatalogCollection.videos),
            ).order_by(CatalogCollection.local_title)).all())
            rows = []
            for collection in collections:
                summaries = [
                    summarize_title_numbering(list(title.videos))
                    for title in collection.titles
                ]
                numbering_unknown = sum(summary.unknown for summary in summaries)
                if (
                    collection.hierarchy_status in {"review_required", "conflict"}
                    or collection_requires_numbering_review(collection)
                ):
                    rows.append({
                        "collection": collection,
                        "numbering_unknown": numbering_unknown,
                    })
        return templates.TemplateResponse(request, "hierarchy_review.html", {
            "rows": rows,
        })

    @app.get("/hierarchy-review/{collection_id}", response_class=HTMLResponse)
    def hierarchy_review_detail(
        request: Request, collection_id: int, message: str | None = None,
    ):
        return hierarchy_review_context(request, collection_id, message=message)

    def metadata_review_context(request: Request, status: str = "without", batch_result=None):
        allowed = {"without", "pending", "manual", "conflict", "missing-artwork", "low-score"}
        if status not in allowed:
            raise HTTPException(status_code=404, detail="Neznámý přehled metadat")
        with sessions() as session:
            titles = list(session.scalars(select(CatalogTitle).options(
                selectinload(CatalogTitle.collection), selectinload(CatalogTitle.metadata_record),
                selectinload(CatalogTitle.metadata_candidates), selectinload(CatalogTitle.artwork),
            ).order_by(CatalogTitle.local_title)).all())
        rows = []
        for title in titles:
            active = [candidate for candidate in title.metadata_candidates if candidate.rejected_at is None]
            best = max((candidate.match_score or 0 for candidate in active), default=None)
            include = {
                "without": title.metadata_status in {"unlinked", "unavailable", "error"},
                "pending": title.metadata_status == "candidates_available",
                "manual": title.metadata_status == "linked_manual",
                "conflict": title.metadata_status == "conflict",
                "missing-artwork": title.metadata_status == "linked_manual" and not any(item.is_primary for item in title.artwork),
                "low-score": any((candidate.match_score or 0) < LOW_SCORE_THRESHOLD for candidate in active),
            }[status]
            if include:
                rows.append({"title": title, "candidate_count": len(active), "best_score": best,
                             "last_search": max((candidate.updated_at for candidate in title.metadata_candidates), default=None)})
        return templates.TemplateResponse(request, "metadata_review.html", {
            "rows": rows, "status": status, "batch_result": batch_result,
            "default_batch_limit": settings.metadata_batch_search_limit,
        })

    @app.get("/metadata-review", response_class=HTMLResponse)
    def metadata_review(request: Request, status: str = "without"):
        return metadata_review_context(request, status)

    @app.post("/metadata/batch-search", response_class=HTMLResponse)
    def batch_metadata_search(request: Request, limit: int = Form(10)):
        safe_limit = max(1, min(limit, settings.metadata_batch_search_limit))
        result = batch_search_candidates(
            sessions, app.state.metadata_provider, limit=safe_limit,
            candidate_limit=settings.metadata_candidate_limit,
            throttle=lambda: time.sleep(0.25),
        )
        return metadata_review_context(request, "pending", result)

    @app.post("/hierarchy-review/{collection_id}/preview", response_class=HTMLResponse)
    def hierarchy_review_preview(
        request: Request, collection_id: int, definitions_json: str = Form(...),
    ):
        try:
            definitions = parse_manual_definitions(definitions_json)
            with sessions() as session:
                collection = session.scalar(select(CatalogCollection).options(
                    selectinload(CatalogCollection.videos)
                ).where(CatalogCollection.id == collection_id))
                if collection is None:
                    raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
                preview = preview_assignments(collection.videos, definitions)
            return hierarchy_review_context(
                request, collection_id, definitions_json, preview
            )
        except ValueError as exc:
            return hierarchy_review_context(
                request, collection_id, definitions_json, error=str(exc)
            )

    @app.post("/hierarchy-review/{collection_id}/simple-preview", response_class=HTMLResponse)
    async def hierarchy_review_simple_preview(request: Request, collection_id: int):
        form = await request.form()
        columns = {
            field: [str(value) for value in form.getlist(field)]
            for field in SIMPLE_DEFINITION_FIELDS
        }
        row_count = len(columns["title_id"])
        simple_rows = [
            {field: columns[field][index] for field in SIMPLE_DEFINITION_FIELDS}
            for index in range(row_count)
        ] if row_count and all(len(values) == row_count for values in columns.values()) else []
        try:
            if not simple_rows:
                raise ValueError("Jednoduchý formulář částí není úplný.")
            definitions = parse_simple_definitions(simple_rows)
            definitions_json = definitions_to_json(definitions)
            with sessions() as session:
                collection = session.scalar(select(CatalogCollection).options(
                    selectinload(CatalogCollection.videos)
                ).where(CatalogCollection.id == collection_id))
                if collection is None:
                    raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
                preview = preview_assignments(collection.videos, definitions)
            return hierarchy_review_context(
                request, collection_id, definitions_json, preview,
                simple_rows=simple_rows,
            )
        except ValueError as exc:
            return hierarchy_review_context(
                request, collection_id, error=str(exc), simple_rows=simple_rows or None,
            )

    @app.post("/hierarchy-review/{collection_id}/metadata-search", response_class=HTMLResponse)
    def hierarchy_review_metadata_search(
        request: Request, collection_id: int, metadata_query: str = Form(...),
    ):
        if not settings.metadata_enabled or not settings.anilist_enabled:
            return hierarchy_review_context(
                request, collection_id, error="Vyhledávání metadat je vypnuté."
            )
        try:
            provider = AniListProvider(settings.metadata_request_timeout_seconds)
            candidates = provider.search_titles(metadata_query)
            return hierarchy_review_context(
                request, collection_id, external_search_candidates=candidates
            )
        except (ValueError, MetadataProviderError) as exc:
            return hierarchy_review_context(request, collection_id, error=str(exc))

    @app.post("/hierarchy-review/{collection_id}/apply")
    def hierarchy_review_apply(
        collection_id: int, definitions_json: str = Form(...),
        confirm_conflicts: bool = Form(False),
    ):
        with sessions() as session:
            try:
                definitions = parse_manual_definitions(definitions_json)
                apply_manual_split(
                    session, collection_id, definitions,
                    confirm_conflicts=confirm_conflicts,
                )
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            f"/hierarchy-review/{collection_id}#result", status_code=303
        )

    @app.post("/hierarchy-review/{collection_id}/status")
    def hierarchy_review_status(
        collection_id: int, hierarchy_status: str = Form(...),
        hierarchy_note: str = Form(""),
    ):
        allowed = {"automatic", "review_required", "verified", "conflict", "not_applicable"}
        if hierarchy_status not in allowed:
            raise HTTPException(status_code=400, detail="Neplatný stav hierarchie.")
        note = hierarchy_note.strip()[:1000] or None
        with sessions() as session:
            collection = session.get(CatalogCollection, collection_id)
            if collection is None:
                raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
            collection.hierarchy_status = hierarchy_status
            collection.hierarchy_note = note
            collection.hierarchy_verified_at = (
                utc_now() if hierarchy_status in {"verified", "not_applicable"} else None
            )
            session.commit()
        return RedirectResponse(f"/hierarchy-review/{collection_id}", status_code=303)

    @app.post("/hierarchy-review/{collection_id}/season-one")
    def hierarchy_review_set_season_one(
        collection_id: int, confirm_single_season: bool = Form(False),
    ):
        if not confirm_single_season:
            raise HTTPException(
                status_code=400,
                detail="Nastavení Season 1 je nutné explicitně potvrdit.",
            )
        with sessions() as session:
            collection = session.scalar(select(CatalogCollection).options(
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_record),
            ).where(CatalogCollection.id == collection_id))
            if collection is None:
                raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
            try:
                apply_single_season_suggestion(collection)
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': 'Season 1 byla předvyplněna; zařazení ani hierarchie zatím nejsou ověřené.'})}",
            status_code=303,
        )

    @app.post("/collections/{collection_id}/titles/{catalog_title_id}/hierarchy")
    def update_title_hierarchy(
        collection_id: int, catalog_title_id: int,
        season_number_manual: str = Form(""), season_label_manual: str = Form(""),
        part_type_manual: str = Form(""), sort_order_manual: str = Form(""),
        hierarchy_verified: bool = Form(False), filter_name: str = Form("all"),
        q: str = Form(""), sort: str = Form(""), direction: str = Form(""),
    ):
        allowed_types = {"", "title", "season", "part", "cour", "film", "ova", "special"}
        with sessions() as session:
            title = session.get(CatalogTitle, catalog_title_id)
            if title is None or title.catalog_collection_id != collection_id:
                raise HTTPException(status_code=404, detail="Část kolekce nebyla nalezena")
            try:
                number = int(season_number_manual) if season_number_manual.strip() else None
                order = int(sort_order_manual) if sort_order_manual.strip() else None
                label = season_label_manual.strip() or None
                part_type = part_type_manual.strip().casefold() or None
                if number is not None and number <= 0:
                    raise ValueError("Pořadí sezóny musí být kladné číslo.")
                if order is not None and order < 0:
                    raise ValueError("Pořadí části nesmí být záporné.")
                if label and len(label) > 50:
                    raise ValueError("Označení části může mít nejvýše 50 znaků.")
                if (part_type or "") not in allowed_types:
                    raise ValueError("Neplatný typ části.")
                has_manual = any(value is not None for value in (number, label, part_type, order))
                if has_manual and not hierarchy_verified:
                    raise ValueError("Ruční hierarchii je nutné potvrdit jako ověřenou.")
                title.season_number_manual = number
                title.season_label_manual = label
                title.part_type_manual = part_type
                title.sort_order_manual = order
                title.hierarchy_manual_override = has_manual
                title.hierarchy_verified_at = utc_now() if hierarchy_verified else None
                recalculate_title_numbering(title, list(title.videos))
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        params = {"filter_name": filter_name, "q": q, "sort": sort, "direction": direction}
        return RedirectResponse(
            f"/collections/{collection_id}?{urlencode(params)}#title-{catalog_title_id}",
            status_code=303,
        )

    @app.post("/catalog/{filter_name}/titles/{catalog_title_id}/metadata/search", response_class=HTMLResponse)
    def search_metadata(
        request: Request, filter_name: str, catalog_title_id: int,
        metadata_query: str = Form(...), q: str = Form(""), sort: str = Form(""),
        direction: str = Form(""), video_sort: str = Form(""),
        video_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        candidates, metadata_error = [], None
        with sessions() as session:
            catalog_title = _load_catalog_title(session, catalog_title_id)
            if catalog_title is None:
                raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
            if not settings.metadata_enabled or not settings.anilist_enabled:
                metadata_error = "Vyhledávání metadat je vypnuté v konfiguraci."
            else:
                try:
                    candidates = search_and_store_candidates(
                        session, catalog_title, metadata_query, app.state.metadata_provider,
                        limit=settings.metadata_candidate_limit,
                    )
                    session.commit()
                    catalog_title = _load_catalog_title(session, catalog_title_id)
                except (ValueError, MetadataProviderError) as exc:
                    session.rollback()
                    metadata_error = str(exc)
                    catalog_title = _load_catalog_title(session, catalog_title_id)
        all_videos = _load_videos(sessions)
        results = build_catalog_results(all_videos, filter_name, q, sort, direction)
        title_videos_for_detail = [
            video for video in all_videos
            if video.catalog_title_id == catalog_title.id
            and video_matches_filter(video, filter_name)
        ]
        videos, normalized_video_sort, normalized_video_direction = sort_title_videos(
            title_videos_for_detail,
            video_sort, video_direction,
        )
        if not videos:
            raise HTTPException(status_code=404, detail="Titul neodpovídá aktivnímu filtru")
        def video_sort_url(column: str) -> str:
            return series_state_url(
                filter_name, catalog_title.id, results.query, results.sort, results.direction,
                column, toggled_direction(column, normalized_video_sort, normalized_video_direction),
            )
        context = {
            "filter_name": filter_name, "filter_label": FILTER_LABELS[filter_name],
            "series": determine_parent_series(videos[0].relative_path),
            "catalog_title": catalog_title, "videos": videos,
            "translation_status": translation_status, "video_matches_filter": video_matches_filter,
            "derive_season_info": derive_season_info, "derive_episode_number": derive_episode_number,
            "q": results.query, "sort": results.sort, "direction": results.direction,
            "video_sort": normalized_video_sort, "video_direction": normalized_video_direction,
            "video_sort_url": video_sort_url,
            "back_url": (
                f"/collections/{catalog_title.catalog_collection_id}?"
                f"{urlencode({'filter_name': filter_name, 'q': results.query, 'sort': results.sort, 'direction': results.direction})}"
                if catalog_title.catalog_collection_id
                else catalog_state_url(filter_name, results.query, results.sort, results.direction)
            ),
            "metadata_status_labels": METADATA_STATUS_LABELS,
            "metadata_error": metadata_error,
            "metadata_query": metadata_query[:200],
            "metadata_message": None, "pending_external_id": None,
            "metadata_warning": None,
            "require_conflict_confirmation": False,
            "require_locked_confirmation": False,
            "metadata_allow_remote_images": settings.metadata_allow_remote_images,
            # Ručně odeslaný dotaz se znovu nenormalizuje.
            "metadata_default_query": metadata_query,
        }
        context.update(_metadata_template_values(
            catalog_title, settings.metadata_allow_remote_images
        ))
        return templates.TemplateResponse(request, "series.html", context)

    def action_redirect(
        filter_name: str, catalog_title_id: int, q: str, sort: str, direction: str,
        detail_sort: str, detail_direction: str, **messages: str,
    ):
        return RedirectResponse(metadata_return_url(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction, **messages,
        ), status_code=303)

    @app.post("/catalog/{filter_name}/titles/{catalog_title_id}/metadata/confirm")
    def confirm_metadata(
        filter_name: str, catalog_title_id: int, external_id: str = Form(...),
        candidate_id: int | None = Form(None),
        confirm_conflict: bool = Form(False), confirm_locked: bool = Form(False),
        q: str = Form(""), sort: str = Form(""), direction: str = Form(""),
        detail_sort: str = Form(""), detail_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        with sessions() as session:
            title = session.get(CatalogTitle, catalog_title_id)
            if title is None:
                raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
            if not settings.metadata_enabled or not settings.anilist_enabled:
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction,
                    metadata_error="AniList metadata jsou vypnutá v konfiguraci.",
                )
            try:
                confirm_anilist_candidate(
                    session, title, external_id, app.state.metadata_provider,
                    confirm_conflict=confirm_conflict, confirm_locked=confirm_locked,
                    candidate_id=candidate_id,
                )
                session.commit()
            except MetadataConflictError as exc:
                session.rollback()
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction, metadata_error=str(exc),
                    pending_external_id=external_id,
                    require_conflict_confirmation="true",
                    require_locked_confirmation="true" if confirm_locked else "",
                )
            except MetadataLockedError as exc:
                session.rollback()
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction, metadata_error=str(exc),
                    pending_external_id=external_id,
                    require_locked_confirmation="true",
                    require_conflict_confirmation="true" if confirm_conflict else "",
                )
            except (ValueError, MetadataProviderError) as exc:
                session.rollback()
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction, metadata_error=str(exc),
                )
        artwork_warning = cache_title_artwork(catalog_title_id)
        return action_redirect(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction, message="Metadata byla ručně potvrzena.",
            metadata_warning=artwork_warning or "",
        )

    @app.post("/catalog/{filter_name}/titles/{catalog_title_id}/metadata/update")
    def update_metadata(
        filter_name: str, catalog_title_id: int, q: str = Form(""),
        sort: str = Form(""), direction: str = Form(""),
        detail_sort: str = Form(""), detail_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        with sessions() as session:
            title = session.get(CatalogTitle, catalog_title_id)
            if title is None:
                raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
            if not settings.metadata_enabled or not settings.anilist_enabled:
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction,
                    metadata_error="AniList metadata jsou vypnutá v konfiguraci.",
                )
            try:
                refresh_title_metadata(session, title, app.state.metadata_provider)
                session.commit()
            except (ValueError, MetadataLockedError, MetadataProviderError) as exc:
                session.rollback()
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction, metadata_error=str(exc),
                )
        artwork_warning = cache_title_artwork(catalog_title_id)
        return action_redirect(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction, message="Metadata byla aktualizována.",
            metadata_warning=artwork_warning or "",
        )

    @app.post("/catalog/{filter_name}/titles/{catalog_title_id}/metadata/artwork/refresh")
    def refresh_artwork(
        filter_name: str, catalog_title_id: int, q: str = Form(""),
        sort: str = Form(""), direction: str = Form(""),
        detail_sort: str = Form(""), detail_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        with sessions() as session:
            if session.get(CatalogTitle, catalog_title_id) is None:
                raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
        warning = cache_title_artwork(catalog_title_id, force=True)
        return action_redirect(
            filter_name, catalog_title_id, q, sort, direction, detail_sort, detail_direction,
            message="Obal byl obnoven." if not warning else "",
            metadata_warning=warning or "",
        )

    @app.post("/catalog/{filter_name}/titles/{catalog_title_id}/metadata/candidates/{candidate_id}/reject")
    def reject_candidate(
        filter_name: str, catalog_title_id: int, candidate_id: int,
        rejected: bool = Form(True), q: str = Form(""), sort: str = Form(""),
        direction: str = Form(""), detail_sort: str = Form(""), detail_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        with sessions() as session:
            if session.get(CatalogTitle, catalog_title_id) is None:
                raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
            try:
                set_candidate_rejected(session, catalog_title_id, candidate_id, rejected)
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return action_redirect(
            filter_name, catalog_title_id, q, sort, direction, detail_sort, detail_direction,
            message="Kandidát byl odmítnut." if rejected else "Odmítnutí kandidáta bylo zrušeno.",
            show_rejected="true" if not rejected else "",
        )

    @app.post("/catalog/{filter_name}/titles/{catalog_title_id}/metadata/unlink")
    def unlink_metadata(
        filter_name: str, catalog_title_id: int, confirm_unlink: bool = Form(False),
        confirm_locked: bool = Form(False), q: str = Form(""), sort: str = Form(""),
        direction: str = Form(""), detail_sort: str = Form(""),
        detail_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        with sessions() as session:
            title = session.get(CatalogTitle, catalog_title_id)
            if title is None:
                raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
            if not confirm_unlink:
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction,
                    metadata_error="Odpojení je nutné výslovně potvrdit.",
                )
            if title.metadata_locked and not confirm_locked:
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction,
                    metadata_error="Metadata jsou zamknutá; potvrďte i odpojení zamknutých metadat.",
                )
            unlink_title_metadata(session, title)
            session.commit()
        return action_redirect(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction, message="Metadata byla odpojena.",
        )

    @app.post("/catalog/{filter_name}/titles/{catalog_title_id}/metadata/lock")
    def set_metadata_lock(
        filter_name: str, catalog_title_id: int, locked: bool = Form(...),
        q: str = Form(""), sort: str = Form(""), direction: str = Form(""),
        detail_sort: str = Form(""), detail_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        with sessions() as session:
            title = session.get(CatalogTitle, catalog_title_id)
            if title is None:
                raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
            title.metadata_locked = locked
            session.commit()
        return action_redirect(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction,
            message="Metadata byla zamknuta." if locked else "Metadata byla odemknuta.",
        )

    @app.post("/catalog/{filter_name}/titles/{catalog_title_id}/display-title")
    def update_display_title(
        filter_name: str, catalog_title_id: int, manual_display_title: str = Form(""),
        q: str = Form(""), sort: str = Form(""), direction: str = Form(""),
        detail_sort: str = Form(""), detail_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        with sessions() as session:
            title = session.get(CatalogTitle, catalog_title_id)
            if title is None:
                raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
            try:
                set_manual_display_title(session, title, manual_display_title)
                session.commit()
            except ValueError as exc:
                session.rollback()
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction, metadata_error=str(exc),
                )
        return action_redirect(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction, message="Zobrazovaný název byl uložen.",
        )

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

    @app.post("/titles/{catalog_title_id}/numbering")
    def update_title_numbering(
        catalog_title_id: int, numbering_mode: str = Form("auto"),
        episode_start_offset: str = Form(""), filter_name: str = Form("all"),
        q: str = Form(""), sort: str = Form(""), direction: str = Form(""),
        detail_sort: str = Form(""), detail_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=400, detail="Neplatný filtr")
        with sessions() as session:
            title = session.get(CatalogTitle, catalog_title_id)
            if title is None:
                raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
            try:
                offset = int(episode_start_offset) if episode_start_offset.strip() else None
                set_title_numbering(
                    title, "unknown" if numbering_mode == "auto" else numbering_mode, offset
                )
                recalculate_title_numbering(title, list(title.videos))
                session.commit()
            except ValueError as exc:
                session.rollback()
                return RedirectResponse(metadata_return_url(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction, numbering_error=str(exc),
                ).replace("#metadata", "#numbering"), status_code=303)
        return RedirectResponse(metadata_return_url(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction, numbering_message="Číslování bylo uloženo.",
        ).replace("#metadata", "#numbering"), status_code=303)

    @app.post("/videos/{video_id}/episode-number")
    def update_video_episode_number(
        video_id: int, manual_episode_number: str = Form(""),
        filter_name: str = Form("all"), q: str = Form(""), sort: str = Form(""),
        direction: str = Form(""), detail_sort: str = Form(""),
        detail_direction: str = Form(""),
    ):
        with sessions() as session:
            video = session.get(Video, video_id)
            if video is None or video.catalog_title_id is None:
                raise HTTPException(status_code=404, detail="Video nebylo nalezeno")
            try:
                value = int(manual_episode_number) if manual_episode_number.strip() else None
                set_video_episode_override(video, value)
                title = video.catalog_title
                recalculate_title_numbering(title, list(title.videos))
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            metadata_return_url(
                filter_name, video.catalog_title_id, q, sort, direction,
                detail_sort, detail_direction,
            ).replace("#metadata", f"#video-{video_id}"),
            status_code=303,
        )

    @app.post("/titles/{catalog_title_id}/numbering/sequence")
    def apply_title_numbering_sequence(
        catalog_title_id: int, sequence_start: int = Form(...),
        confirm_apply: bool = Form(False),
        confirm_manual_conflicts: bool = Form(False),
        filter_name: str = Form("all"), q: str = Form(""),
        sort: str = Form(""), direction: str = Form(""),
        detail_sort: str = Form(""), detail_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=400, detail="Neplatný filtr")
        with sessions() as session:
            title = session.scalar(select(CatalogTitle).options(
                selectinload(CatalogTitle.videos),
            ).where(CatalogTitle.id == catalog_title_id))
            if title is None:
                raise HTTPException(status_code=404, detail="Titul nebyl nalezen")
            try:
                if not confirm_apply:
                    raise ValueError("Sekvenční číslování je nutné explicitně potvrdit.")
                apply_sequential_numbering(
                    list(title.videos), sequence_start,
                    confirm_manual_conflicts=confirm_manual_conflicts,
                )
                recalculate_title_numbering(title, list(title.videos))
                session.commit()
            except ValueError as exc:
                session.rollback()
                return RedirectResponse(metadata_return_url(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction,
                    numbering_error=str(exc), sequence_start=str(sequence_start),
                ).replace("#metadata", "#numbering"), status_code=303)
        return RedirectResponse(metadata_return_url(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction,
            numbering_message="Sekvenční číslování bylo uloženo.",
        ).replace("#metadata", "#numbering"), status_code=303)

    @app.post("/scan")
    def scan(confirm_deletions: bool = Form(False)):
        try:
            with sessions() as session:
                result = scan_library(
                    session,
                    settings.anime_path,
                    require_mount=settings.require_mount,
                    confirm_deletions=confirm_deletions,
                    ffprobe_timeout_seconds=settings.ffprobe_timeout_seconds,
                    library_access_timeout_seconds=settings.library_access_timeout_seconds,
                    library_healthcheck_interval_files=settings.library_healthcheck_interval_files,
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
