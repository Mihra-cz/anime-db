from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import ceil
from typing import Literal, Mapping

from .catalog import (
    AudioStatus,
    VideoLanguageProfile,
    build_video_language_profile,
    catalog_collection_display_title,
    catalog_title_display_title,
    catalog_title_series_label,
    detect_episode_number,
    effective_external_subtitle_language,
    is_media_completion_video,
    manual_hardsub_state,
    natural_sort_key,
    normalize_search_query,
)
from .external_subtitle_compatibility import (
    VideoExternalSubtitleState,
    build_video_external_subtitle_states,
)
from .models import Video


CZSK_AVAILABILITY_UNAVAILABLE = "unavailable"
MEDIA_CHECK_PAGE_SIZE = 50

MediaCheckSubtitleStatus = Literal[
    "available",
    "needs_cs_sk_internal_en",
    "needs_cs_sk_no_fallback",
    "needs_cs_sk_compatibility_unknown",
    "known_unavailable_internal_en",
    "known_unavailable_no_fallback",
]
MediaCheckSeverity = Literal["success", "warning", "error", "info"]

SUBTITLE_FILTER_LABELS: Mapping[str, str] = {
    "all": "Vše",
    "unresolved": "Doplnit CZ/SK",
    "unresolved-internal-en": "Doplnit CZ/SK – Internal EN",
    "unresolved-no-fallback": "Doplnit CZ/SK – bez fallbacku",
    "unavailable": "CZ/SK nejsou dostupné",
    "available": "CZ/SK hotovo",
}
AUDIO_FILTER_LABELS: Mapping[str, str] = {
    "all": "Všechna audia",
    "unknown": "Jazyk audia neurčen",
    "english_only": "Pouze EN dab",
    "other_known": "Jiný dab",
    "no_audio": "Bez audio stopy",
    "japanese": "JP audio",
}
SUBTITLE_STATUS_LABELS: Mapping[MediaCheckSubtitleStatus, str] = {
    "available": "CZ/SK dostupné",
    "needs_cs_sk_internal_en": "Doplnit CZ/SK · Internal EN",
    "needs_cs_sk_no_fallback": "Doplnit CZ/SK · bez vhodných titulků",
    "needs_cs_sk_compatibility_unknown": (
        "Kompatibilita CZ/SK titulku neposouzena"
    ),
    "known_unavailable_internal_en": "CZ/SK nyní nejsou dostupné · Internal EN",
    "known_unavailable_no_fallback": "CZ/SK nyní nejsou dostupné · bez fallbacku",
}
AUDIO_STATUS_LABELS: Mapping[AudioStatus, str] = {
    "japanese": "JP audio",
    "english_only": "Pouze EN dab",
    "other_known": "Bez JP audia · jiný dab",
    "unknown": "Jazyk audia neurčen",
    "no_audio": "Bez audio stopy",
}
AUDIO_STATUS_SEVERITY: Mapping[AudioStatus, MediaCheckSeverity] = {
    "japanese": "success",
    "english_only": "info",
    "other_known": "info",
    "unknown": "warning",
    "no_audio": "error",
}


@dataclass(frozen=True)
class MediaCheckEvaluation:
    factual: VideoLanguageProfile
    subtitle_status: MediaCheckSubtitleStatus
    subtitle_severity: MediaCheckSeverity
    subtitle_is_open: bool
    manual_unavailable_recorded: bool
    manual_unavailable_effective: bool
    hardsub_review_recommended: bool
    completion_required: bool
    has_unknown_cs_sk_candidate: bool


@dataclass(frozen=True)
class MediaCheckRow:
    video: Video
    evaluation: MediaCheckEvaluation
    collection_name: str
    title_name: str
    hierarchy_label: str
    episode_label: str
    external_subtitle_state: VideoExternalSubtitleState


@dataclass(frozen=True)
class MediaCheckResults:
    rows: tuple[MediaCheckRow, ...]
    subtitle_counts: Mapping[str, int]
    audio_counts: Mapping[str, int]
    subtitle_filter: str
    audio_filter: str
    query: str
    page: int
    page_size: int
    total_filtered: int
    total_pages: int

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


def set_czsk_availability_manual(video: Video, value: str | None) -> None:
    """Persist only the explicit workflow decision, never factual availability."""
    normalized = (value or "").strip().casefold()
    if not normalized:
        video.czsk_availability_manual = None
        return
    if normalized != CZSK_AVAILABILITY_UNAVAILABLE:
        raise ValueError("Neplatné ruční rozhodnutí o dostupnosti CZ/SK titulků.")
    video.czsk_availability_manual = CZSK_AVAILABILITY_UNAVAILABLE


def build_media_check_evaluation(
    video: Video,
    *,
    external_subtitle_state: VideoExternalSubtitleState | None = None,
) -> MediaCheckEvaluation:
    """Combine Commit-7 facts with the independent Media Check decision."""
    factual = build_video_language_profile(video)
    manual_recorded = (
        video.czsk_availability_manual == CZSK_AVAILABILITY_UNAVAILABLE
    )
    has_unknown_cs_sk_candidate = bool(
        external_subtitle_state
        and any(
            effective_external_subtitle_language(subtitle) in {"cs", "sk"}
            for subtitle in external_subtitle_state.unknown_candidate_subtitles
        )
    )
    if factual.subtitle_status == "preferred":
        subtitle_status: MediaCheckSubtitleStatus = "available"
        severity: MediaCheckSeverity = "success"
        is_open = False
        manual_effective = False
    elif manual_recorded:
        subtitle_status = (
            "known_unavailable_internal_en"
            if factual.subtitle_status == "fallback_internal_en"
            else "known_unavailable_no_fallback"
        )
        severity = "info"
        is_open = False
        manual_effective = True
    elif has_unknown_cs_sk_candidate:
        subtitle_status = "needs_cs_sk_compatibility_unknown"
        severity = "warning"
        is_open = True
        manual_effective = False
    elif factual.subtitle_status == "fallback_internal_en":
        subtitle_status = "needs_cs_sk_internal_en"
        severity = "warning"
        is_open = True
        manual_effective = False
    else:
        subtitle_status = "needs_cs_sk_no_fallback"
        severity = "error"
        is_open = True
        manual_effective = False

    return MediaCheckEvaluation(
        factual=factual,
        subtitle_status=subtitle_status,
        subtitle_severity=severity,
        subtitle_is_open=is_open,
        manual_unavailable_recorded=manual_recorded,
        manual_unavailable_effective=manual_effective,
        hardsub_review_recommended=(
            factual.subtitle_status != "preferred"
            and manual_hardsub_state(video) == "unknown"
        ),
        completion_required=is_media_completion_video(video),
        has_unknown_cs_sk_candidate=has_unknown_cs_sk_candidate,
    )


def _subtitle_matches(evaluation: MediaCheckEvaluation, filter_name: str) -> bool:
    status = evaluation.subtitle_status
    if filter_name != "all" and not evaluation.completion_required:
        return False
    return {
        "all": True,
        "unresolved": evaluation.subtitle_is_open,
        "unresolved-internal-en": status == "needs_cs_sk_internal_en",
        "unresolved-no-fallback": status == "needs_cs_sk_no_fallback",
        "unavailable": evaluation.manual_unavailable_effective,
        "available": status == "available",
    }[filter_name]


def _audio_matches(evaluation: MediaCheckEvaluation, filter_name: str) -> bool:
    if filter_name != "all" and not evaluation.completion_required:
        return False
    return filter_name == "all" or evaluation.factual.audio_status == filter_name


def _episode_label(video: Video) -> str:
    if video.season_episode_number is not None:
        return f"E{video.season_episode_number}"
    detection = detect_episode_number(video.filename)
    if detection.display_value is None:
        return "—"
    if detection.kind in {"standard", "fractional", "zero"}:
        return f"E{detection.display_value}"
    return detection.display_value


def _build_row(
    video: Video,
    title_name_preference: object,
    external_subtitle_state: VideoExternalSubtitleState,
) -> MediaCheckRow:
    title = video.catalog_title
    collection = title.collection if title is not None else video.catalog_collection
    collection_name = (
        catalog_collection_display_title(
            collection, title_name_preference, titles=collection.titles,
        )
        if collection is not None else video.root_folder
    )
    title_name = (
        catalog_title_display_title(title, title_name_preference, videos=())
        if title is not None else "Nezařazené video"
    )
    return MediaCheckRow(
        video=video,
        evaluation=build_media_check_evaluation(
            video, external_subtitle_state=external_subtitle_state,
        ),
        collection_name=collection_name,
        title_name=title_name,
        hierarchy_label=catalog_title_series_label(title) if title is not None else "—",
        episode_label=_episode_label(video),
        external_subtitle_state=external_subtitle_state,
    )


def _row_matches_search(row: MediaCheckRow, query: str) -> bool:
    if not query:
        return True
    folded = query.casefold()
    return any(
        folded in (value or "").casefold()
        for value in (
            row.collection_name,
            row.title_name,
            row.video.filename,
            row.video.relative_path,
            row.hierarchy_label,
            row.episode_label,
            *(
                subtitle.relative_path
                for subtitle in (
                    row.external_subtitle_state.compatible_subtitles
                    + row.external_subtitle_state.incompatible_subtitles
                    + row.external_subtitle_state.unknown_candidate_subtitles
                )
            ),
        )
    )


def _row_sort_key(row: MediaCheckRow) -> tuple:
    detection = detect_episode_number(row.video.filename)
    episode_value = (
        Decimal(row.video.season_episode_number)
        if row.video.season_episode_number is not None
        else detection.sortable_episode_value
    )
    return (
        natural_sort_key(row.collection_name),
        natural_sort_key(row.title_name),
        episode_value is None,
        episode_value or Decimal(0),
        natural_sort_key(row.video.filename),
        row.video.id or 0,
    )


def _subtitle_counts(rows: list[MediaCheckRow]) -> dict[str, int]:
    return {
        filter_name: sum(
            _subtitle_matches(row.evaluation, filter_name) for row in rows
        )
        for filter_name in SUBTITLE_FILTER_LABELS
    }


def _audio_counts(rows: list[MediaCheckRow]) -> dict[str, int]:
    return {
        filter_name: sum(_audio_matches(row.evaluation, filter_name) for row in rows)
        for filter_name in AUDIO_FILTER_LABELS
    }


def build_media_check_results(
    videos: list[Video],
    *,
    subtitle_filter: str = "unresolved",
    audio_filter: str = "all",
    query: str | None = None,
    page: int = 1,
    page_size: int = MEDIA_CHECK_PAGE_SIZE,
    title_name_preference: object = "romaji",
    external_subtitle_states: Mapping[
        int, VideoExternalSubtitleState
    ] | None = None,
) -> MediaCheckResults:
    """Build faceted counts, filtering and pagination from one workflow evaluator."""
    if subtitle_filter not in SUBTITLE_FILTER_LABELS:
        raise ValueError("Neznámý Media Check filtr titulků.")
    if audio_filter not in AUDIO_FILTER_LABELS:
        raise ValueError("Neznámý Media Check filtr audia.")
    if page_size < 1:
        raise ValueError("Velikost stránky musí být kladná.")

    normalized_query = normalize_search_query(query)
    states = (
        external_subtitle_states
        if external_subtitle_states is not None
        else build_video_external_subtitle_states(videos)
    )
    empty_state = VideoExternalSubtitleState((), (), ())
    searched = sorted(
        (
            row for row in (
                _build_row(
                    video,
                    title_name_preference,
                    states.get(video.id, empty_state),
                )
                for video in videos
            )
            if _row_matches_search(row, normalized_query)
        ),
        key=_row_sort_key,
    )
    subtitle_basis = [
        row for row in searched if _audio_matches(row.evaluation, audio_filter)
    ]
    audio_basis = [
        row for row in searched if _subtitle_matches(row.evaluation, subtitle_filter)
    ]
    filtered = [
        row for row in searched
        if _subtitle_matches(row.evaluation, subtitle_filter)
        and _audio_matches(row.evaluation, audio_filter)
    ]

    total_filtered = len(filtered)
    total_pages = max(1, ceil(total_filtered / page_size))
    normalized_page = min(max(1, page), total_pages)
    start = (normalized_page - 1) * page_size
    return MediaCheckResults(
        rows=tuple(filtered[start:start + page_size]),
        subtitle_counts=_subtitle_counts(subtitle_basis),
        audio_counts=_audio_counts(audio_basis),
        subtitle_filter=subtitle_filter,
        audio_filter=audio_filter,
        query=normalized_query,
        page=normalized_page,
        page_size=page_size,
        total_filtered=total_filtered,
        total_pages=total_pages,
    )
