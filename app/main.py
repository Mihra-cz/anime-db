from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import time
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .catalog import (
    FILTER_LABELS,
    ROOT_FOLDER,
    ROOT_VIDEO_GROUP_LABEL,
    TITLE_NAME_PREFERENCE_LABELS,
    build_catalog_results,
    catalog_title_display_title,
    catalog_title_series_label,
    detect_episode_number,
    derive_episode_number,
    derive_season_info,
    determine_parent_series,
    effective_video_content_display,
    group_videos_by_series,
    has_meaningful_root_assignment,
    is_film_video,
    is_root_video,
    manual_hardsub_state,
    normalize_search_query,
    normalize_title_name_preference,
    normalize_title,
    set_manual_hardsub,
    sort_title_videos,
    subtitle_track_display,
    title_videos,
    translation_status,
    unresolved_duplicate_video_ids,
    video_matches_filter,
    video_matches_search,
)
from .config import Settings, get_settings
from .database import Base, make_engine, make_session_factory
from .migrations import migrate_schema
from .hierarchy_review import (
    PERIOD_HINT_REVIEW_REASON, SIMPLE_DEFINITION_FIELDS,
    apply_manual_split, apply_single_title_confirmation,
    classify_videos_in_place, clear_confirmed_duplicate_videos,
    collection_grouping_suggestions, create_main_collection,
    confirm_duplicate_groups, confirm_duplicate_videos, create_title_from_videos,
    delete_empty_collection, delete_empty_collections, delete_empty_local_title,
    definitions_as_json, definitions_to_json, parse_manual_definitions,
    confirm_effective_collection_hierarchy,
    catalog_title_hierarchy_is_verified, manual_hierarchy_resolves_ambiguity,
    hierarchy_review_diagnostics, manual_hierarchy_snapshot_issue,
    merge_title_into, move_videos_to_title,
    move_titles_to_collection, record_grouping_decision,
    parse_simple_definitions,
    refresh_collection_state,
    preview_assignments, separate_nonstandard_videos, simple_definition_rows,
    set_manual_duplicate_status,
    single_title_confirmation_suggestion, supplementary_assignment_recommendations,
    supplementary_video_suggestions, set_manual_title_hierarchy,
)
from .hierarchy_types import PART_TYPE_CHOICES, VIDEO_CONTENT_TYPE_CHOICES
from .metadata.providers.anilist import AniListProvider
from .metadata.providers.base import MetadataProviderError
from .metadata.artwork import ArtworkCacheError, cache_cover
from .metadata.candidates import (
    LOW_SCORE_THRESHOLD, batch_search_candidates, decode_match_reasons, search_and_store_candidates,
    set_candidate_rejected,
)
from .media_parts import (
    MEDIA_PART_NUMBER_ERROR, media_part_label, media_part_ordinal_warning,
    media_part_sequence_warning, media_part_summary_label, set_media_part_number,
)
from .metadata.service import (
    MetadataConflictError, MetadataLockedError, confirm_anilist_candidate,
    default_metadata_search_query, normalize_metadata_search_query, refresh_title_metadata,
    set_manual_display_title, unlink_title_metadata,
)
from .models import CatalogCollection, CatalogTitle, ExternalTitleLink, TitleMetadata, Video, utc_now
from .numbering import (
    apply_sequential_numbering,
    confirmed_duplicate_groups, preview_sequential_numbering,
    effective_video_numbering,
    recalculate_title_numbering, set_title_numbering, set_video_episode_override,
    summarize_title_numbering, unresolved_duplicate_groups,
)
from .scanner import LibrarySafetyError, scan_library

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent
PREFERRED_TITLE_LANGUAGE_COOKIE = "animedb_preferred_title_language"
CANONICAL_TITLE_NAME_PREFERENCES = {
    preference: preference for preference in TITLE_NAME_PREFERENCE_LABELS
}


def safe_local_redirect_target(target: object) -> str:
    """Vrátí pouze lokální absolute-path URL, jinak bezpečný fallback."""
    if not isinstance(target, str):
        return "/"
    candidate = target.strip()
    normalized_candidate = candidate.replace("\\", "/")
    if (
        not candidate
        or normalized_candidate != candidate
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        return "/"
    try:
        parsed = urlparse(normalized_candidate)
    except ValueError:
        return "/"
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or normalized_candidate.startswith("//")
    ):
        return "/"
    return normalized_candidate


def local_redirect_response(target: object, *, status_code: int = 303) -> RedirectResponse:
    """Vytvoří redirect s explicitně vynuceným lokálním cílem."""
    return RedirectResponse(safe_local_redirect_target(target), status_code=status_code)


def get_preferred_title_language(request: Request) -> str:
    """Vrátí současný zdroj preference odděleně od display-title resolveru."""
    settings = getattr(request.app.state, "settings", None)
    default = getattr(settings, "preferred_title_language", "romaji")
    return normalize_title_name_preference(
        request.cookies.get(PREFERRED_TITLE_LANGUAGE_COOKIE), default
    )


def _template_preferences(request: Request) -> dict:
    query = request.url.query
    return {
        "title_name_preference": get_preferred_title_language(request),
        "title_name_preference_labels": TITLE_NAME_PREFERENCE_LABELS,
        "preference_return_to": request.url.path + (f"?{query}" if query else ""),
    }


templates = Jinja2Templates(
    directory=PACKAGE_DIR / "templates", context_processors=[_template_preferences]
)
templates.env.globals.update(
    catalog_title_display_title=catalog_title_display_title,
    catalog_title_series_label=catalog_title_series_label,
    catalog_title_hierarchy_is_verified=catalog_title_hierarchy_is_verified,
    manual_hierarchy_snapshot_issue=manual_hierarchy_snapshot_issue,
    subtitle_track_display=subtitle_track_display,
    manual_hardsub_state=manual_hardsub_state,
    detect_episode_number=detect_episode_number,
    effective_video_content_display=effective_video_content_display,
    part_type_choices=PART_TYPE_CHOICES,
    video_content_type_choices=VIDEO_CONTENT_TYPE_CHOICES,
    media_part_label=media_part_label,
    media_part_ordinal_warning=media_part_ordinal_warning,
    media_part_summary_label=media_part_summary_label,
)
METADATA_STATUS_LABELS = {
    "unlinked": "Bez metadat", "candidates_available": "Čeká na potvrzení",
    "linked_auto": "Spárováno automaticky", "linked_manual": "Spárováno ručně",
    "conflict": "Konflikt", "migration_review_required": "Vyžaduje kontrolu migrace",
    "unavailable": "Bez externího záznamu", "error": "Chyba",
}


def _homepage_collection_rows(
    videos: list[Video], collection_title_ids: dict[int, tuple[int, ...]],
    title_name_preference: object = "romaji",
) -> list[dict]:
    """Sestaví navigační homepage nad uloženou logickou hierarchií."""
    results = build_catalog_results(
        videos, "all", sort="title", direction="asc",
        title_name_preference=title_name_preference,
    )
    rows = []
    for group in results.groups:
        if group.is_root_group or group.catalog_collection_id is None:
            continue
        title_ids = collection_title_ids.get(group.catalog_collection_id, ())
        group_videos = results.videos_by_title[group.relative_path]
        sole_title_id = title_ids[0] if len(title_ids) == 1 else None
        has_unambiguous_title = bool(
            sole_title_id
            and group_videos
            and all(video.catalog_title_id == sole_title_id for video in group_videos)
        )
        rows.append({
            "group": group,
            "href": (
                f"/titles/{sole_title_id}"
                if has_unambiguous_title
                else f"/collections/{group.catalog_collection_id}"
            ),
            "title_count": len(title_ids),
            "opens_title": has_unambiguous_title,
        })
    return rows


def _load_videos(sessions) -> list[Video]:
    with sessions() as session:
        return list(session.scalars(select(Video).options(
            selectinload(Video.audio_tracks), selectinload(Video.internal_subtitles),
            selectinload(Video.external_subtitles),
            selectinload(Video.catalog_title).selectinload(
                CatalogTitle.collection
            ).selectinload(CatalogCollection.titles),
            selectinload(Video.catalog_title).selectinload(CatalogTitle.metadata_record),
            selectinload(Video.catalog_collection).selectinload(CatalogCollection.titles),
            selectinload(Video.duplicate_of),
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
        selectinload(CatalogTitle.videos),
    ).where(CatalogTitle.id == catalog_title_id))


def _hierarchy_video_groups(videos: list[Video]) -> dict[str, list]:
    standard, supplemental, nonstandard, unknown, duplicates = [], [], [], [], []
    for video in sorted(
        videos,
        key=lambda item: (
            item.season_episode_number is None,
            item.season_episode_number or 0,
            item.filename.casefold(),
        ),
    ):
        numbering = effective_video_numbering(video)
        detection = numbering.detection
        if (
            video.duplicate_of_video_id is not None or video.duplicate_of is not None
            or video.duplicate_primary_missing
        ):
            duplicates.append({"video": video, "detection": detection})
        elif numbering.is_supplementary:
            supplemental.append({"video": video, "detection": detection})
        elif numbering.is_nonstandard:
            nonstandard.append({"video": video, "detection": detection})
        elif numbering.is_standard and numbering.season_episode_number is not None:
            standard.append({"video": video, "detection": detection})
        else:
            unknown.append({"video": video, "detection": detection})
    return {
        "standard": standard, "supplemental": supplemental,
        "nonstandard": nonstandard, "unknown": unknown, "duplicates": duplicates,
    }


def _duplicate_video_details(video: Video) -> dict[str, str]:
    audio = ", ".join(
        f"{track.language or 'unknown'} / {track.codec or 'unknown'}"
        for track in video.audio_tracks
    ) or "—"
    internal = ", ".join(
        f"{track.normalized_language or track.language or 'unknown'} / {track.codec or 'unknown'}"
        for track in video.internal_subtitles
    ) or "—"
    external = ", ".join(
        track.normalized_language or track.language or "unknown"
        for track in video.external_subtitles
    ) or "—"
    hardsub = manual_hardsub_state(video)
    return {
        "duration": (
            f"{int(video.duration // 60):02}:{int(video.duration % 60):02}"
            if video.duration is not None else "—"
        ),
        "resolution": f"{video.width}×{video.height}" if video.width and video.height else "—",
        "codec": video.video_codec or "—",
        "audio": audio,
        "subtitles": f"interní: {internal}; externí: {external}",
        "hardsub": {"yes": "ano", "no": "ne", "unknown": "neznámé"}[hardsub],
        "size": f"{video.size / (1024 ** 3):.2f} GiB",
    }


def _video_display_rows(
    videos: list[Video], known_video_ids: set[int] | None = None,
) -> list[dict]:
    included_video_ids = {video.id for video in videos}
    available_video_ids = (
        included_video_ids if known_video_ids is None else known_video_ids
    )
    duplicate_copies_by_primary: dict[int, list[Video]] = {}
    for video in videos:
        if video.duplicate_of_video_id in included_video_ids:
            duplicate_copies_by_primary.setdefault(
                video.duplicate_of_video_id, []
            ).append(video)
    return [
        {
            "video": video,
            "duplicate_copies": duplicate_copies_by_primary.get(video.id, []),
            "orphan_duplicate": bool(
                video.duplicate_primary_missing
                or video.duplicate_of_video_id is not None
                and video.duplicate_of_video_id not in available_video_ids
            ),
        }
        for video in videos
        if video.duplicate_of_video_id not in included_video_ids
    ]


templates.env.globals.update(video_display_rows=_video_display_rows)


def _metadata_template_values(
    title: CatalogTitle | None,
    allow_remote_images: bool,
    show_rejected: bool = False,
    show_candidates: bool = False,
) -> dict:
    metadata = title.metadata_record if title else None
    def decoded(value: str | None) -> list:
        try:
            result = json.loads(value or "[]")
            return result if isinstance(result, list) else []
        except (TypeError, ValueError):
            return []
    artwork = next((item for item in (title.artwork if title else []) if item.is_primary and item.artwork_type == "cover"), None)
    stored_candidates = sorted(
        [item for item in (title.metadata_candidates if title else []) if show_rejected or item.rejected_at is None],
        key=lambda item: (item.rejected_at is not None, -(item.match_score or 0), item.candidate_title.casefold()),
    )
    primary_external_link = next(
        (link for link in (title.external_links if title else []) if link.is_primary), None
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
        "primary_external_link": primary_external_link,
        "metadata_candidates": stored_candidates if show_candidates else [],
        "candidate_reasons": {
            item.id: decode_match_reasons(item) for item in stored_candidates
        },
        "has_metadata_candidates": bool(stored_candidates),
        "show_metadata_candidates": show_candidates,
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
        "anime_titles", "total", "episodes", "films", "bonus", "cs", "sk",
        "only_cs", "only_sk", "both_cs_sk", "translated", "missing", "unknown",
    )}


def _add_video(
    stats: dict[str, int], video: Video, *, separate_films: bool = False,
) -> None:
    status = translation_status(video)
    stats["total"] += 1
    if separate_films and is_film_video(video):
        stats["films"] += 1
    else:
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

    @app.post("/preferences/title-name")
    def update_title_name_preference(
        preference: str = Form(...), return_to: str = Form("/"),
    ):
        normalized = preference.strip().casefold()
        canonical_preference = CANONICAL_TITLE_NAME_PREFERENCES.get(normalized)
        if canonical_preference is None:
            raise HTTPException(status_code=400, detail="Neplatná varianta názvu.")
        response = local_redirect_response(return_to)
        response.set_cookie(
            PREFERRED_TITLE_LANGUAGE_COOKIE, canonical_preference, max_age=31_536_000,
            httponly=True, samesite="lax",
        )
        return response

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
                selectinload(Video.internal_subtitles), selectinload(Video.external_subtitles),
                selectinload(Video.catalog_title).selectinload(CatalogTitle.collection),
                selectinload(Video.catalog_title).selectinload(CatalogTitle.metadata_record),
                selectinload(Video.catalog_collection),
            )).all()
            collection_title_ids = {
                collection.id: tuple(title.id for title in collection.titles)
                for collection in session.scalars(select(CatalogCollection).options(
                    selectinload(CatalogCollection.titles)
                )).all()
            }
        folders: dict[str, dict[str, int]] = {}
        totals = _empty_stats()
        collection_rows = _homepage_collection_rows(
            videos,
            collection_title_ids,
            get_preferred_title_language(request),
        )
        totals["anime_titles"] = len(collection_rows)
        for video in videos:
            if not (is_root_video(video) and has_meaningful_root_assignment(video)):
                _add_video(folders.setdefault(video.root_folder, _empty_stats()), video)
            _add_video(totals, video, separate_films=True)
        return templates.TemplateResponse(request, "index.html", {
            "collections": collection_rows,
            "folders": sorted(folders.items()), "totals": totals, "message": message,
            "error": error, "confirm_deletions": confirm_deletions,
            "q": normalize_search_query(q),
        })

    @app.get("/folders/{folder:path}", response_class=HTMLResponse)
    def folder_detail(request: Request, folder: str):
        if folder in {"", ROOT_FOLDER}:
            return RedirectResponse("/root-videos", status_code=307)
        videos = [video for video in _load_videos(sessions) if video.root_folder == folder]
        results = build_catalog_results(
            videos, "all",
            title_name_preference=get_preferred_title_language(request),
        )
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

    @app.get("/root-videos", response_class=HTMLResponse)
    def root_videos(request: Request):
        title_name_preference = get_preferred_title_language(request)
        videos = sorted(
            [
                video for video in _load_videos(sessions)
                if is_root_video(video) and not has_meaningful_root_assignment(video)
            ],
            key=lambda video: video.filename.casefold(),
        )
        with sessions() as session:
            target_titles = list(session.scalars(select(CatalogTitle).options(
                selectinload(CatalogTitle.collection),
                selectinload(CatalogTitle.metadata_record),
                selectinload(CatalogTitle.videos),
            ).join(CatalogTitle.collection).where(
                CatalogCollection.relative_root_path != ROOT_FOLDER
            ).order_by(CatalogCollection.local_title, CatalogTitle.local_title)).all())
        rows = []
        for video in videos:
            meaningful_assignment = has_meaningful_root_assignment(video)
            rows.append({
                "video": video,
                "display_title": (
                    catalog_title_display_title(
                        video.catalog_title, title_name_preference, videos=[video],
                    )
                    if meaningful_assignment and video.catalog_title
                    else Path(video.filename).stem
                ),
                "meaningful_assignment": meaningful_assignment,
                "metadata": (
                    video.catalog_title.metadata_record
                    if video.catalog_title else None
                ),
            })
        return templates.TemplateResponse(request, "root_videos.html", {
            "rows": rows,
            "target_titles": target_titles,
            "root_group_label": ROOT_VIDEO_GROUP_LABEL,
            "metadata_status_labels": METADATA_STATUS_LABELS,
            "subtitle_track_display": subtitle_track_display,
            "manual_hardsub_state": manual_hardsub_state,
        })

    @app.post("/root-videos/{video_id}/assignment")
    def assign_root_video(video_id: int, target_title_id: str = Form("")):
        with sessions() as session:
            video = session.get(Video, video_id)
            if video is None or not is_root_video(video):
                raise HTTPException(status_code=404, detail="Root video nebylo nalezeno")
            old_title = video.catalog_title
            target_title = None
            if target_title_id.strip():
                try:
                    parsed_title_id = int(target_title_id)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="Neplatný cílový titul") from exc
                target_title = session.get(CatalogTitle, parsed_title_id)
                if (
                    target_title is None or target_title.collection is None
                    or target_title.collection.relative_root_path == ROOT_FOLDER
                ):
                    raise HTTPException(status_code=400, detail="Cílový titul nelze použít")
                video.catalog_title = target_title
                video.catalog_collection = target_title.collection
            else:
                video.catalog_title = None
                video.catalog_collection = None
            session.flush()
            for title in {old_title, target_title} - {None}:
                recalculate_title_numbering(title, list(title.videos))
            session.commit()
        return local_redirect_response(f"/root-videos#video-{video_id}")

    @app.post("/root-videos/{video_id}/new-title")
    def create_root_video_title(
        video_id: int, display_title: str = Form(...), part_type: str = Form("film")
    ):
        name = display_title.strip()
        if not name or len(name) > 200:
            raise HTTPException(status_code=400, detail="Název musí mít 1 až 200 znaků")
        labels = {"film": "Film", "ova": "OVA", "special": "Special"}
        if part_type not in labels:
            raise HTTPException(status_code=400, detail="Neplatný typ titulu")
        with sessions() as session:
            video = session.get(Video, video_id)
            if video is None or not is_root_video(video):
                raise HTTPException(status_code=404, detail="Root video nebylo nalezeno")
            old_title = video.catalog_title
            virtual_root = f"@root/{video.id}"
            collection = session.scalar(select(CatalogCollection).where(
                CatalogCollection.relative_root_path == virtual_root
            ))
            if collection is None:
                collection = CatalogCollection(
                    local_title=name, normalized_local_title=normalize_title(name),
                    relative_root_path=virtual_root, manual_display_title=name,
                    hierarchy_status="review_required",
                    hierarchy_note="Samostatný titul ručně vytvořený pro video v kořeni knihovny.",
                )
                session.add(collection)
                session.flush()
            title_path = f"{virtual_root}/title"
            title = session.scalar(select(CatalogTitle).where(
                CatalogTitle.relative_root_path == title_path
            ))
            if title is None:
                title = CatalogTitle(
                    collection=collection, local_title=name,
                    normalized_local_title=normalize_title(name),
                    relative_root_path=title_path,
                )
                session.add(title)
            collection.local_title = name
            collection.normalized_local_title = normalize_title(name)
            collection.manual_display_title = name
            title.local_title = name
            title.normalized_local_title = normalize_title(name)
            title.manual_display_title = name
            title.part_type_manual = part_type
            title.season_label_manual = labels[part_type]
            title.hierarchy_manual_override = True
            video.catalog_collection = collection
            video.catalog_title = title
            session.flush()
            for affected_title in {old_title, title} - {None}:
                recalculate_title_numbering(affected_title, list(affected_title.videos))
            session.commit()
        return local_redirect_response(f"/root-videos#video-{video_id}")

    @app.get("/catalog/{filter_name}", response_class=HTMLResponse)
    def catalog(
        request: Request, filter_name: str, q: str = "",
        sort: str | None = None, direction: str | None = None,
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        videos = _load_videos(sessions)
        results = build_catalog_results(
            videos, filter_name, q, sort, direction,
            title_name_preference=get_preferred_title_language(request),
        )
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
        show_metadata_candidates: bool = False,
        metadata_query: str | None = None,
        media_part_message: str | None = None,
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        all_videos = _load_videos(sessions)
        results = build_catalog_results(
            all_videos, filter_name, q, sort, direction,
            title_name_preference=get_preferred_title_language(request),
        )
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
            return local_redirect_response(
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
        detail_unresolved_duplicate_ids = unresolved_duplicate_video_ids(title_candidates)
        filtered_candidates = [
            video for video in title_candidates
            if video_matches_filter(
                video, filter_name,
                unresolved_duplicate_ids=detail_unresolved_duplicate_ids,
            )
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
        if not videos and catalog_title is None:
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
            "series": determine_parent_series(
                videos[0].relative_path if videos else
                f"{catalog_title.relative_root_path}/.empty-title"
            ),
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
            "metadata_query": metadata_query or (
                default_metadata_search_query(catalog_title) if catalog_title else ""
            ),
            "videos": videos,
            "title_media_videos": title_candidates,
            "media_part_summary": media_part_summary_label(title_candidates),
            "media_part_sequence_warning": media_part_sequence_warning(
                title_candidates
            ),
            "media_part_message": media_part_message,
            "title_is_empty": bool(catalog_title and not title_candidates),
            "title_owned_metadata_count": (
                bool(catalog_title.metadata_record)
                + len(catalog_title.external_links)
                + len(catalog_title.metadata_candidates)
                + len(catalog_title.artwork)
                if catalog_title else 0
            ),
            "catalog_title_display_title": catalog_title_display_title,
            "catalog_title_series_label": catalog_title_series_label,
            "subtitle_track_display": subtitle_track_display,
            "manual_hardsub_state": manual_hardsub_state,
            "translation_status": translation_status,
            "video_matches_filter": lambda video, selected_filter: video_matches_filter(
                video, selected_filter,
                unresolved_duplicate_ids=detail_unresolved_duplicate_ids,
            ),
            "unresolved_duplicate_video_ids": detail_unresolved_duplicate_ids,
            "title_video_ids": {
                video.id for video in title_candidates if video.id is not None
            },
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
        context["metadata_change_url"] = metadata_return_url(
            filter_name,
            catalog_title.id if catalog_title else 0,
            results.query,
            results.sort,
            results.direction,
            normalized_video_sort,
            normalized_video_direction,
            show_metadata_candidates="true",
        )
        context["metadata_rejected_url"] = metadata_return_url(
            filter_name,
            catalog_title.id if catalog_title else 0,
            results.query,
            results.sort,
            results.direction,
            normalized_video_sort,
            normalized_video_direction,
            show_metadata_candidates="true",
            show_rejected="false" if show_rejected else "true",
            metadata_query=context["metadata_query"],
        )
        context.update(_metadata_template_values(
            catalog_title,
            settings.metadata_allow_remote_images,
            show_rejected,
            show_metadata_candidates,
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
        show_metadata_candidates: bool = False,
        metadata_query: str | None = None,
        media_part_message: str | None = None,
    ):
        return series_detail(
            request, filter_name, catalog_title_id, None, q, sort, direction,
            video_sort, video_direction, message, metadata_error, metadata_warning, show_rejected, pending_external_id,
            require_conflict_confirmation, require_locked_confirmation,
            sequence_start, numbering_error, numbering_message,
            show_metadata_candidates, metadata_query, media_part_message,
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
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
            ).where(CatalogCollection.id == collection_id))
        if collection is None:
            raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
        all_videos = _load_videos(sessions)
        all_unresolved_duplicate_ids = unresolved_duplicate_video_ids(all_videos)
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
                video for video in title_videos_list
                if video_matches_filter(
                    video, filter_name,
                    unresolved_duplicate_ids=all_unresolved_duplicate_ids,
                )
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
                parts.append({
                    "title": title, "stats": stats, "metadata": title.metadata_record,
                    "videos": title_videos_list,
                })
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
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_candidates),
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.artwork),
                selectinload(CatalogCollection.videos).selectinload(Video.audio_tracks),
                selectinload(CatalogCollection.videos).selectinload(Video.internal_subtitles),
                selectinload(CatalogCollection.videos).selectinload(Video.external_subtitles),
                selectinload(CatalogCollection.videos).selectinload(Video.duplicate_of),
            ).where(CatalogCollection.id == collection_id))
            if collection is None:
                raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
            videos = sorted(collection.videos, key=lambda video: video.relative_path)
            review_diagnostics = hierarchy_review_diagnostics(collection, videos)
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
                state.numbering_input for video in videos
                if (state := effective_video_numbering(video)).is_standard
                and state.numbering_input is not None
            ]
            external_candidates = [
                {"title": title, "metadata": title.metadata_record, "links": title.external_links}
                for title in collection.titles
                if title.metadata_record or title.external_links
            ]
            title_numbering = []
            for title in sorted(
                collection.titles,
                key=lambda value: (
                    value.effective_sort_order,
                    value.local_title.casefold(),
                ),
            ):
                title_videos_list = [
                    video for video in videos if video.catalog_title_id == title.id
                ]
                title_card_issues = review_diagnostics.for_title_card(title)
                title_numbering.append({
                    "title": title,
                    "summary": summarize_title_numbering(
                        title_videos_list, title
                    ),
                    "videos": _hierarchy_video_groups(title_videos_list),
                    "unresolved_duplicate_groups": unresolved_duplicate_groups(
                        title_videos_list
                    ),
                    "confirmed_duplicate_groups": confirmed_duplicate_groups(
                        title_videos_list
                    ),
                    "metadata_linked": bool(
                        title.metadata_record
                        and any(link.is_primary for link in title.external_links)
                    ),
                    "can_delete": bool(
                        not title.videos
                    ),
                    "manual_split_definition": title.hierarchy_manual_override,
                    "owned_metadata_count": (
                        bool(title.metadata_record)
                        + len(title.external_links)
                        + len(title.metadata_candidates)
                        + len(title.artwork)
                    ),
                    "long_flat_review": any(
                        issue.code == "long_flat_sequence"
                        for issue in title_card_issues
                    ),
                    "diagnostic_issues": title_card_issues,
                    "has_blocking_issue": any(
                        issue.blocking for issue in title_card_issues
                    ),
                })
            numbering_unknown = sum(
                item["summary"].unknown for item in title_numbering
            )
            nonstandard_videos = [
                {"video": video, "detection": state.detection}
                for video in videos
                if (state := effective_video_numbering(video)).is_nonstandard
            ]
            unassigned_videos = _hierarchy_video_groups([
                video for video in videos if video.catalog_title_id is None
            ])
            part_confirmation = single_title_confirmation_suggestion(collection)
            part_confirmation_summary = next((
                item["summary"] for item in title_numbering
                if part_confirmation is not None
                and item["title"].id == part_confirmation.title.id
            ), None)
            part_confirmation_long_flat = any(
                item["long_flat_review"]
                for item in title_numbering
                if part_confirmation is not None
                and item["title"].id == part_confirmation.title.id
            )
            duplicate_candidate_video_ids = {
                video.id
                for item in title_numbering
                for group in item["unresolved_duplicate_groups"]
                for video in group.videos
            }
            confirmed_duplicate_video_ids = {
                video.id
                for item in title_numbering
                for group in item["confirmed_duplicate_groups"]
                for video in group.videos
            }
            supplementary_suggestions = supplementary_video_suggestions(
                videos, include_video_ids=duplicate_candidate_video_ids,
            )
            assignment_recommendations = supplementary_assignment_recommendations(videos)
            assignment_recommendation_by_video = {
                item.video.id: recommendation
                for recommendation in assignment_recommendations
                for item in recommendation.items
            }
            supplementary_suggestions = tuple(
                suggestion for suggestion in supplementary_suggestions
                if suggestion.video.id not in assignment_recommendation_by_video
            )
            supplementary_suggestion_by_video = {
                suggestion.video.id: suggestion
                for suggestion in supplementary_suggestions
            }
            available_collections = list(session.scalars(select(CatalogCollection).where(
                CatalogCollection.id != collection.id
            ).order_by(CatalogCollection.local_title)).all())
            return templates.TemplateResponse(request, "hierarchy_review_detail.html", {
                "collection": collection, "videos": videos,
                "review_diagnostics": review_diagnostics,
                "unassigned_diagnostic_issues": tuple(
                    issue for issue in review_diagnostics.issues
                    if issue.scope == "video"
                    and any(
                        video.catalog_title_id is None
                        and video.catalog_title is None
                        for video in issue.videos
                    )
                ),
                "episode_min": min(episode_numbers) if episode_numbers else None,
                "episode_max": max(episode_numbers) if episode_numbers else None,
                "definitions_json": definitions_json, "preview": preview,
                "preview_rows": preview_rows, "error": error,
                "external_candidates": external_candidates,
                "external_search_candidates": external_search_candidates or [],
                "metadata_status_labels": METADATA_STATUS_LABELS,
                "title_numbering": title_numbering,
                "numbering_unknown": numbering_unknown,
                "nonstandard_videos": nonstandard_videos,
                "unassigned_videos": unassigned_videos,
                "part_confirmation_suggestion": part_confirmation,
                "part_confirmation_summary": part_confirmation_summary,
                "part_confirmation_long_flat": part_confirmation_long_flat,
                "supplementary_suggestions": supplementary_suggestions,
                "supplementary_suggestion_by_video": supplementary_suggestion_by_video,
                "assignment_recommendations": assignment_recommendations,
                "assignment_recommendation_by_video": assignment_recommendation_by_video,
                "duplicate_candidate_video_ids": duplicate_candidate_video_ids,
                "confirmed_duplicate_video_ids": confirmed_duplicate_video_ids,
                "available_collections": available_collections,
                "duplicate_video_details": {
                    video.id: _duplicate_video_details(video) for video in videos
                },
                "simple_rows": simple_rows or simple_definition_rows(collection),
                "message": message,
            })

    @app.get("/hierarchy-review", response_class=HTMLResponse)
    def hierarchy_review_list(request: Request, message: str | None = None):
        with sessions() as session:
            collections = list(session.scalars(select(CatalogCollection).options(
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_record),
            ).order_by(CatalogCollection.local_title)).all())
            video_counts = dict(session.execute(
                select(Video.catalog_collection_id, func.count(Video.id))
                .where(Video.catalog_collection_id.is_not(None))
                .group_by(Video.catalog_collection_id)
            ).all())
            rows = []
            for collection in collections:
                summaries = [
                    summarize_title_numbering(list(title.videos), title)
                    for title in collection.titles
                ]
                numbering_unknown = sum(summary.unknown for summary in summaries)
                if (
                    collection.hierarchy_status in {"review_required", "conflict"}
                    or any(summary.requires_review for summary in summaries)
                ):
                    rows.append({
                        "collection": collection,
                        "numbering_unknown": numbering_unknown,
                        "video_count": video_counts.get(collection.id, 0),
                    })
            suggestions = collection_grouping_suggestions(
                session, collections=collections,
            )
            selectable_collections = collections
            empty_collections = [
                collection for collection in collections
                if not collection.titles and not video_counts.get(collection.id, 0)
            ]
        return templates.TemplateResponse(request, "hierarchy_review.html", {
            "rows": rows, "suggestions": suggestions,
            "selectable_collections": selectable_collections,
            "empty_collections": empty_collections,
            "message": message,
        })

    @app.get("/hierarchy-review/{collection_id}", response_class=HTMLResponse)
    def hierarchy_review_detail(
        request: Request, collection_id: int, message: str | None = None,
    ):
        return hierarchy_review_context(request, collection_id, message=message)

    def current_grouping_suggestion(session, key: str):
        suggestion = next(
            (item for item in collection_grouping_suggestions(session) if item.key == key),
            None,
        )
        if suggestion is None:
            raise ValueError("Návrh už neodpovídá aktuálnímu stavu; načtěte přehled znovu.")
        return suggestion

    @app.post("/hierarchy-review/collections/create")
    async def hierarchy_review_create_collection(request: Request):
        form = await request.form()
        try:
            title_ids = [int(value) for value in form.getlist("title_ids")]
            suggestion_key = str(form.get("suggestion_key") or "").strip()
            with sessions() as session:
                suggestion = (
                    current_grouping_suggestion(session, suggestion_key)
                    if suggestion_key else None
                )
                collection = create_main_collection(
                    session, str(form.get("local_title") or ""), title_ids,
                )
                if suggestion is not None:
                    record_grouping_decision(session, suggestion, "merged")
                session.commit()
                collection_id = collection.id
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = "Hlavní collection byla vytvořena a vybrané části logicky přesunuty."
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': message})}",
        )

    @app.post("/hierarchy-review/collections/move")
    async def hierarchy_review_move_titles(request: Request):
        form = await request.form()
        try:
            title_ids = [int(value) for value in form.getlist("title_ids")]
            target_id = int(str(form.get("target_collection_id") or ""))
            suggestion_key = str(form.get("suggestion_key") or "").strip()
            with sessions() as session:
                suggestion = (
                    current_grouping_suggestion(session, suggestion_key)
                    if suggestion_key else None
                )
                move_titles_to_collection(session, target_id, title_ids)
                if suggestion is not None:
                    record_grouping_decision(session, suggestion, "merged")
                session.commit()
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = "Vybrané CatalogTitle byly přesunuty; fyzické cesty zůstaly beze změny."
        return local_redirect_response(
            f"/hierarchy-review/{target_id}?{urlencode({'message': message})}",
        )

    @app.post("/hierarchy-review/grouping/keep-separate")
    def hierarchy_review_keep_grouping_separate(suggestion_key: str = Form(...)):
        with sessions() as session:
            try:
                suggestion = current_grouping_suggestion(session, suggestion_key)
                record_grouping_decision(session, suggestion, "separate")
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/hierarchy-review", status_code=303)

    @app.post("/hierarchy-review/collections/delete-empty")
    def hierarchy_review_delete_empty_collection(
        collection_id: int = Form(...), confirm_delete: bool = Form(False),
    ):
        if not confirm_delete:
            raise HTTPException(status_code=400, detail="Odstranění je nutné potvrdit.")
        with sessions() as session:
            try:
                delete_empty_collection(session, collection_id)
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/hierarchy-review", status_code=303)

    @app.post("/hierarchy-review/collections/delete-empty-bulk")
    async def hierarchy_review_delete_empty_collections(request: Request):
        form = await request.form()
        if str(form.get("confirm_delete") or "").casefold() not in {"true", "on", "1"}:
            raise HTTPException(
                status_code=400, detail="Hromadné odstranění je nutné potvrdit."
            )
        try:
            collection_ids = [int(value) for value in form.getlist("collection_ids")]
            with sessions() as session:
                result = delete_empty_collections(session, collection_ids)
                session.commit()
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        deleted = ", ".join(name for _, name in result.deleted) or "žádné"
        skipped = ", ".join(name for _, name in result.skipped) or "žádné"
        message = f"Odstraněné collections: {deleted}. Přeskočené: {skipped}."
        return local_redirect_response(
            f"/hierarchy-review?{urlencode({'message': message})}",
        )

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
        message = "Ruční rozdělení bylo úspěšně aplikováno."
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': message})}"
            "#operation-result"
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
            if hierarchy_status == "verified":
                try:
                    confirm_effective_collection_hierarchy(collection)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            collection.hierarchy_status = hierarchy_status
            collection.hierarchy_note = (
                None
                if hierarchy_status == "verified"
                and note == PERIOD_HINT_REVIEW_REASON
                and manual_hierarchy_resolves_ambiguity(collection)
                else note
            )
            collection.hierarchy_verified_at = (
                utc_now() if hierarchy_status in {"verified", "not_applicable"} else None
            )
            session.commit()
        return local_redirect_response(f"/hierarchy-review/{collection_id}")

    @app.post("/hierarchy-review/{collection_id}/separate-nonstandard")
    async def hierarchy_review_separate_nonstandard(request: Request, collection_id: int):
        form = await request.form()
        raw_ids = [str(value) for value in form.getlist("video_ids")]
        try:
            video_ids = [int(value) for value in raw_ids]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Neplatný výběr videí.") from exc
        with sessions() as session:
            try:
                raw_season = str(form.get("season_number") or "").strip()
                raw_part = str(form.get("part_number") or "").strip()
                title = separate_nonstandard_videos(
                    session, collection_id, video_ids,
                    local_title=str(form.get("local_title") or ""),
                    part_type=str(form.get("part_type") or ""),
                    season_number=int(raw_season) if raw_season else None,
                    part_number=int(raw_part) if raw_part else None,
                )
                session.commit()
                title_id = title.id
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = "Nestandardní obsah byl logicky oddělen; fyzické cesty zůstaly beze změny."
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': message})}#title-{title_id}",
        )

    @app.post("/hierarchy-review/{collection_id}/manage-videos")
    async def hierarchy_review_manage_videos(request: Request, collection_id: int):
        form = await request.form()
        try:
            video_ids = [int(value) for value in form.getlist("video_ids")]
            operation = str(form.get("operation") or "").strip()
            with sessions() as session:
                if operation == "classify":
                    content_type = str(form.get("content_type") or "")
                    classify_videos_in_place(
                        session, collection_id, video_ids,
                        content_type,
                    )
                    message = (
                        "Ruční klasifikace byla zrušena; obsah se znovu určuje "
                        "automaticky."
                        if not content_type.strip()
                        else (
                            f"Video bylo ručně klasifikováno jako "
                            f"{content_type.strip().casefold()}."
                            if len(video_ids) == 1
                            else f"Vybraná videa byla ručně klasifikována jako "
                            f"{content_type.strip().casefold()}."
                        )
                    )
                elif operation == "move":
                    move_videos_to_title(
                        session, collection_id, video_ids,
                        int(str(form.get("target_title_id") or "")),
                    )
                    message = "Videa byla logicky přesunuta do existující části."
                elif operation == "create":
                    raw_season = str(form.get("season_number") or "").strip()
                    raw_part = str(form.get("part_number") or "").strip()
                    create_title_from_videos(
                        session, collection_id, video_ids,
                        local_title=str(form.get("local_title") or ""),
                        part_type=str(form.get("part_type") or ""),
                        season_number=int(raw_season) if raw_season else None,
                        season_label=str(form.get("season_label") or ""),
                        part_number=int(raw_part) if raw_part else None,
                    )
                    message = "Byla vytvořena nová logická část a vybraná videa do ní přesunuta."
                else:
                    raise ValueError("Vyberte platnou operaci správy zařazení.")
                session.commit()
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': message})}"
            "#operation-result",
        )

    @app.post("/hierarchy-review/{collection_id}/merge-title")
    def hierarchy_review_merge_title(
        collection_id: int, source_title_id: int = Form(...),
        target_title_id: int = Form(...), confirm_merge: bool = Form(False),
    ):
        if not confirm_merge:
            raise HTTPException(status_code=400, detail="Sloučení je nutné explicitně potvrdit.")
        with sessions() as session:
            try:
                merge_title_into(session, collection_id, source_title_id, target_title_id)
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = "Všechna videa byla logicky přesunuta; zdrojová část zůstala prázdná."
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': message})}#title-{source_title_id}",
        )

    @app.post("/hierarchy-review/{collection_id}/duplicates/confirm")
    async def hierarchy_review_confirm_duplicate(request: Request, collection_id: int):
        form = await request.form()
        if str(form.get("confirm_duplicate") or "").casefold() not in {"true", "on", "1"}:
            raise HTTPException(status_code=400, detail="Duplicitu je nutné explicitně potvrdit.")
        try:
            video_ids = [int(value) for value in form.getlist("video_ids")]
            primary_video_id = int(str(form.get("primary_video_id") or ""))
            with sessions() as session:
                confirm_duplicate_videos(
                    session, collection_id, video_ids, primary_video_id,
                )
                session.commit()
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = (
            "Duplicita byla potvrzena. Číslování je rozlišené, fyzická kopie však "
            "stále vyžaduje budoucí cleanup."
        )
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': message})}#current-parts",
        )

    @app.post("/hierarchy-review/{collection_id}/duplicates/confirm-bulk")
    async def hierarchy_review_confirm_duplicates_bulk(
        request: Request, collection_id: int,
    ):
        form = await request.form()
        if str(form.get("confirm_duplicate") or "").casefold() not in {"true", "on", "1"}:
            raise HTTPException(
                status_code=400, detail="Všechny skupiny duplicit je nutné explicitně potvrdit."
            )
        try:
            assignments = []
            group_keys = list(dict.fromkeys(str(value) for value in form.getlist("group_key")))
            for key in group_keys:
                video_ids = [int(value) for value in form.getlist(f"video_ids_{key}")]
                primary_video_id = int(str(form.get(f"primary_{key}") or ""))
                assignments.append((video_ids, primary_video_id))
            with sessions() as session:
                confirm_duplicate_groups(session, collection_id, assignments)
                session.commit()
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = (
            "Skupiny byly potvrzeny. Logické číslování je rozlišené, fyzické "
            "duplicitní kopie však nadále vyžadují cleanup."
        )
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': message})}#current-parts",
        )

    @app.post("/hierarchy-review/{collection_id}/duplicates/clear")
    async def hierarchy_review_clear_duplicate(request: Request, collection_id: int):
        form = await request.form()
        if str(form.get("confirm_clear") or "").casefold() not in {"true", "on", "1"}:
            raise HTTPException(
                status_code=400, detail="Zrušení označení duplicity je nutné potvrdit."
            )
        try:
            video_ids = [int(value) for value in form.getlist("video_ids")]
            with sessions() as session:
                clear_confirmed_duplicate_videos(session, collection_id, video_ids)
                session.commit()
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = "Označení duplicity bylo zrušeno; kolize čísel znovu vyžaduje kontrolu."
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': message})}#current-parts",
        )

    @app.post("/hierarchy-review/{collection_id}/delete-empty-title")
    def hierarchy_review_delete_empty_title(
        collection_id: int, title_id: int = Form(...),
        confirm_delete: bool = Form(False),
        remove_from_manual_split: bool = Form(False),
    ):
        if not confirm_delete:
            raise HTTPException(status_code=400, detail="Odstranění je nutné explicitně potvrdit.")
        with sessions() as session:
            try:
                removed_definition = delete_empty_local_title(
                    session, collection_id, title_id,
                    remove_from_manual_split=remove_from_manual_split,
                )
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = (
            "Prázdná část byla odstraněna i z ručního rozdělení."
            if removed_definition else "Prázdná lokální část byla odstraněna."
        )
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': message})}",
        )

    @app.post("/titles/{catalog_title_id}/delete-empty")
    def title_delete_empty(
        catalog_title_id: int, confirm_delete: bool = Form(False),
        remove_from_manual_split: bool = Form(False),
    ):
        if not confirm_delete:
            raise HTTPException(
                status_code=400, detail="Odstranění prázdné části je nutné potvrdit."
            )
        with sessions() as session:
            title = session.get(CatalogTitle, catalog_title_id)
            if title is None:
                raise HTTPException(status_code=404, detail="Část nebyla nalezena.")
            collection_id = title.catalog_collection_id
            if collection_id is None:
                raise HTTPException(
                    status_code=400, detail="Část není přiřazena ke collection."
                )
            try:
                removed_definition = delete_empty_local_title(
                    session, collection_id, catalog_title_id,
                    remove_from_manual_split=remove_from_manual_split,
                )
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = (
            "Prázdná část byla odstraněna i z ručního rozdělení."
            if removed_definition else "Prázdná část byla odstraněna pouze z databáze."
        )
        return local_redirect_response(
            f"/hierarchy-review?{urlencode({'message': message})}",
        )

    @app.post("/hierarchy-review/{collection_id}/numbering-preview", response_class=HTMLResponse)
    async def hierarchy_review_numbering_preview(request: Request, collection_id: int):
        form = await request.form()
        try:
            video_ids = [int(value) for value in form.getlist("video_ids")]
            start_episode = int(str(form.get("start_episode") or ""))
            with sessions() as session:
                collection = session.scalar(select(CatalogCollection).options(
                    selectinload(CatalogCollection.videos),
                ).where(CatalogCollection.id == collection_id))
                if collection is None:
                    raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
                selected_ids = set(video_ids)
                selected = [video for video in collection.videos if video.id in selected_ids]
                if not selected_ids or len(selected) != len(selected_ids):
                    raise ValueError("Výběr videí není platný.")
                rows = preview_sequential_numbering(selected, start_episode)
                collection_name = collection.local_title
            return templates.TemplateResponse(request, "bulk_numbering_preview.html", {
                "collection_id": collection_id, "collection_name": collection_name,
                "rows": rows, "start_episode": start_episode,
            })
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/hierarchy-review/{collection_id}/numbering-apply")
    async def hierarchy_review_numbering_apply(request: Request, collection_id: int):
        form = await request.form()
        if str(form.get("confirm_apply") or "").casefold() not in {"true", "on", "1"}:
            raise HTTPException(status_code=400, detail="Číslování je nutné explicitně potvrdit.")
        try:
            video_ids = [int(value) for value in form.getlist("video_ids")]
            start_episode = int(str(form.get("start_episode") or ""))
            confirm_conflicts = str(form.get("confirm_manual_conflicts") or "").casefold() in {
                "true", "on", "1",
            }
            with sessions() as session:
                collection = session.scalar(select(CatalogCollection).options(
                    selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
                    selectinload(CatalogCollection.videos),
                ).where(CatalogCollection.id == collection_id))
                if collection is None:
                    raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
                selected_ids = set(video_ids)
                selected = [video for video in collection.videos if video.id in selected_ids]
                if not selected_ids or len(selected) != len(selected_ids):
                    raise ValueError("Výběr videí není platný.")
                apply_sequential_numbering(
                    selected, start_episode, confirm_manual_conflicts=confirm_conflicts,
                )
                refresh_collection_state(collection)
                session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = "Vybraná videa byla očíslována v potvrzeném pořadí."
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': message})}#assignment",
        )

    @app.post("/hierarchy-review/{collection_id}/confirm-part")
    def hierarchy_review_confirm_part(
        collection_id: int, part_type_manual: str = Form(...),
        season_number_manual: str = Form(""), season_label_manual: str = Form(""),
        part_number_manual: str = Form(""),
        confirm_part: bool = Form(False),
    ):
        if not confirm_part:
            raise HTTPException(
                status_code=400,
                detail="Typ a případné číslo části je nutné explicitně potvrdit.",
            )
        with sessions() as session:
            collection = session.scalar(select(CatalogCollection).options(
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_record),
            ).where(CatalogCollection.id == collection_id))
            if collection is None:
                raise HTTPException(status_code=404, detail="Kolekce nebyla nalezena")
            try:
                season_number = (
                    int(season_number_manual) if season_number_manual.strip() else None
                )
                part_number = (
                    int(part_number_manual) if part_number_manual.strip() else None
                )
                title = apply_single_title_confirmation(
                    collection, part_type=part_type_manual,
                    season_number=season_number, season_label=season_label_manual,
                    part_number=part_number,
                )
                session.commit()
                verified = collection.hierarchy_status == "verified"
                confirmed_label = catalog_title_series_label(title)
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return local_redirect_response(
            f"/hierarchy-review/{collection_id}?{urlencode({'message': (
                f'{confirmed_label} byla potvrzena a původní nejednoznačnost hierarchie je vyřešena.'
                if verified else
                f'{confirmed_label} byla potvrzena; další aktuální problém stále vyžaduje kontrolu.'
            )})}",
        )

    @app.post("/collections/{collection_id}/titles/{catalog_title_id}/hierarchy")
    def update_title_hierarchy(
        collection_id: int, catalog_title_id: int,
        season_number_manual: str = Form(""), season_label_manual: str = Form(""),
        part_number_manual: str = Form(""),
        part_type_manual: str = Form(""), sort_order_manual: str = Form(""),
        hierarchy_verified: bool = Form(False), filter_name: str = Form("all"),
        q: str = Form(""), sort: str = Form(""), direction: str = Form(""),
        return_to: str = Form("collection"),
    ):
        with sessions() as session:
            title = session.get(CatalogTitle, catalog_title_id)
            if title is None or title.catalog_collection_id != collection_id:
                raise HTTPException(status_code=404, detail="Část kolekce nebyla nalezena")
            try:
                number = int(season_number_manual) if season_number_manual.strip() else None
                part_number = (
                    int(part_number_manual) if part_number_manual.strip() else None
                )
                order = int(sort_order_manual) if sort_order_manual.strip() else None
                set_manual_title_hierarchy(
                    title, season_number=number, season_label=season_label_manual,
                    part_type=part_type_manual, sort_order=order,
                    hierarchy_verified=hierarchy_verified, part_number=part_number,
                )
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if return_to == "hierarchy_review":
            return local_redirect_response(
                f"/hierarchy-review/{collection_id}#title-{catalog_title_id}",
            )
        params = {"filter_name": filter_name, "q": q, "sort": sort, "direction": direction}
        return local_redirect_response(
            f"/collections/{collection_id}?{urlencode(params)}#title-{catalog_title_id}",
        )

    @app.post("/catalog/{filter_name}/titles/{catalog_title_id}/metadata/search")
    def search_metadata(
        filter_name: str, catalog_title_id: int,
        metadata_query: str = Form(...), q: str = Form(""), sort: str = Form(""),
        direction: str = Form(""), video_sort: str = Form(""),
        video_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=404, detail="Neznámý filtr")
        candidate_count, metadata_error = 0, None
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
                    candidate_count = len(candidates)
                    session.commit()
                except (ValueError, MetadataProviderError) as exc:
                    session.rollback()
                    metadata_error = str(exc)
        messages = {
            "show_metadata_candidates": "true",
            # Ručně odeslaný dotaz se v navazujícím GET znovu nenormalizuje.
            "metadata_query": metadata_query[:200],
        }
        if metadata_error:
            messages["metadata_error"] = metadata_error
        else:
            messages["message"] = (
                f"Vyhledávání dokončeno: nalezeno {candidate_count} kandidátů."
            )
        return action_redirect(
            filter_name, catalog_title_id, q, sort, direction,
            video_sort, video_direction, **messages,
        )

    def action_redirect(
        filter_name: str, catalog_title_id: int, q: str, sort: str, direction: str,
        detail_sort: str, detail_direction: str, **messages: str,
    ):
        return local_redirect_response(metadata_return_url(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction, **messages,
        ))

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
                    show_metadata_candidates="true",
                    require_conflict_confirmation="true",
                    require_locked_confirmation="true" if confirm_locked else "",
                )
            except MetadataLockedError as exc:
                session.rollback()
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction, metadata_error=str(exc),
                    pending_external_id=external_id,
                    show_metadata_candidates="true",
                    require_locked_confirmation="true",
                    require_conflict_confirmation="true" if confirm_conflict else "",
                )
            except (ValueError, MetadataProviderError) as exc:
                session.rollback()
                return action_redirect(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction, metadata_error=str(exc),
                    show_metadata_candidates="true",
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
            show_metadata_candidates="true",
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
        return local_redirect_response(target)

    @app.post("/videos/{video_id}/duplicate-status-manual")
    def update_manual_duplicate_status(
        video_id: int, duplicate_status_manual: str = Form(""),
        return_to: str = Form("/"),
    ):
        with sessions() as session:
            video = session.get(Video, video_id)
            if video is None:
                raise HTTPException(status_code=404, detail="Video nebylo nalezeno")
            try:
                set_manual_duplicate_status(video, duplicate_status_manual or None)
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return local_redirect_response(return_to)

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
                return local_redirect_response(metadata_return_url(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction, numbering_error=str(exc),
                ).replace("#metadata", "#numbering"))
        return local_redirect_response(metadata_return_url(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction, numbering_message="Číslování bylo uloženo.",
        ).replace("#metadata", "#numbering"))

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
                if title.collection is not None:
                    refresh_collection_state(title.collection, recalculate=False)
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return local_redirect_response(
            metadata_return_url(
                filter_name, video.catalog_title_id, q, sort, direction,
                detail_sort, detail_direction,
            ).replace("#metadata", f"#video-{video_id}"),
        )

    @app.post("/videos/{video_id}/media-part")
    def update_video_media_part(
        video_id: int, media_part_number: str = Form(""),
        filter_name: str = Form("all"), q: str = Form(""),
        sort: str = Form(""), direction: str = Form(""),
        detail_sort: str = Form(""), detail_direction: str = Form(""),
    ):
        if filter_name not in FILTER_LABELS:
            raise HTTPException(status_code=400, detail="Neplatný filtr")
        with sessions() as session:
            video = session.get(Video, video_id)
            if video is None or video.catalog_title_id is None:
                raise HTTPException(status_code=404, detail="Video nebylo nalezeno")
            try:
                raw_value = media_part_number.strip()
                try:
                    value = int(raw_value) if raw_value else None
                except ValueError as exc:
                    raise ValueError(MEDIA_PART_NUMBER_ERROR) from exc
                set_media_part_number(video, value)
                catalog_title_id = video.catalog_title_id
                session.commit()
            except ValueError as exc:
                session.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = (
            f"Část média {value} byla uložena."
            if value is not None else "Část média byla z videa odstraněna."
        )
        return local_redirect_response(
            metadata_return_url(
                filter_name, catalog_title_id, q, sort, direction,
                detail_sort, detail_direction, media_part_message=message,
            ).replace("#metadata", f"#video-{video_id}"),
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
                if title.collection is not None:
                    refresh_collection_state(title.collection, recalculate=False)
                session.commit()
            except ValueError as exc:
                session.rollback()
                return local_redirect_response(metadata_return_url(
                    filter_name, catalog_title_id, q, sort, direction,
                    detail_sort, detail_direction,
                    numbering_error=str(exc), sequence_start=str(sequence_start),
                ).replace("#metadata", "#numbering"))
        return local_redirect_response(metadata_return_url(
            filter_name, catalog_title_id, q, sort, direction,
            detail_sort, detail_direction,
            numbering_message="Sekvenční číslování bylo uloženo.",
        ).replace("#metadata", "#numbering"))

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
            return local_redirect_response(f"/?{urlencode({'message': message})}")
        except LibrarySafetyError as exc:
            logger.warning("Sken bezpečnostně přerušen: %s", exc)
            query = {"error": str(exc)}
            if exc.confirmation_allowed:
                query["confirm_deletions"] = "true"
            return local_redirect_response(f"/?{urlencode(query)}")
        except Exception as exc:
            logger.exception("Sken selhal")
            message = f"Sken selhal. Knihovna může být odpojená: {exc}"
            return local_redirect_response(f"/?{urlencode({'error': message})}")

    return app


app = create_app()
