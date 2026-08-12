from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from .catalog import detect_episode_number, natural_sort_key
from .models import CatalogCollection, CatalogTitle, Video

NUMBERING_MODES = {"unknown", "season_local", "absolute", "mixed"}


@dataclass(frozen=True)
class SequentialNumberingRow:
    video_id: int
    filename: str
    current_episode: int | None
    proposed_episode: int
    manual_conflict: bool


@dataclass(frozen=True)
class TitleNumberingSummary:
    total: int
    standard_total: int
    numbered: int
    unknown: int
    nonstandard: int
    episode_min: int | None
    episode_max: int | None
    gaps: tuple[int, ...]
    duplicate_numbers: tuple[int, ...]

    supplemental: bool = False

    @property
    def unnumbered_standard(self) -> int:
        return self.standard_total - self.numbered

    @property
    def requires_review(self) -> bool:
        if self.supplemental:
            return False
        return bool(
            self.unnumbered_standard or self.unknown or self.nonstandard
            or self.gaps or self.duplicate_numbers
        )


def recalculate_title_numbering(
    title: CatalogTitle,
    videos: list[Video],
    *,
    known_preceding_episodes: int | None = None,
    external_linked: bool | None = None,
) -> None:
    detections = [detect_episode_number(video.filename) for video in videos]
    detected = [item.number if item.is_standard else None for item in detections]
    effective_values = [
        video.episode_number_manual_override
        if video.episode_number_manual_override is not None else local
        for video, local in zip(videos, detected)
    ]
    numeric_values = [value for value in effective_values if value is not None]
    explicit_offset = title.episode_start_offset
    effective_part_number = (
        title.season_number_manual
        if title.season_number_manual is not None
        else title.part_number or title.season_number
    )
    inferred_offset = (
        known_preceding_episodes
        if explicit_offset is None and effective_part_number and effective_part_number > 1
        else None
    )
    offset = explicit_offset if explicit_offset is not None else inferred_offset
    local_is_absolute = bool(offset is not None and numeric_values and min(numeric_values) > offset)
    has_external = title.metadata_record is not None if external_linked is None else external_linked

    for video, detection, local, effective in zip(
        videos, detections, detected, effective_values
    ):
        video.local_episode_number = local
        if effective is None:
            video.season_episode_number = None
            video.absolute_episode_number = None
            video.external_episode_number = None
            video.episode_number_source = {
                "zero": "nonstandard_zero",
                "fractional": "fractional",
            }.get(detection.kind, "unknown")
            video.episode_number_confidence = 0.95 if detection.is_nonstandard else None
            continue
        is_manual = video.episode_number_manual_override is not None
        if title.numbering_mode == "absolute":
            absolute = effective
            season = effective - offset if offset is not None and effective > offset else None
        elif title.numbering_mode == "season_local":
            season = effective
            absolute = effective + offset if offset is not None else (
                effective if (effective_part_number or 1) == 1 else None
            )
        elif offset is not None:
            season = effective - offset if local_is_absolute else effective
            absolute = effective if local_is_absolute else effective + offset
        else:
            season = effective
            absolute = effective if (effective_part_number or 1) == 1 else None
        video.season_episode_number = season if season and season > 0 else None
        video.absolute_episode_number = absolute if absolute and absolute > 0 else None
        video.external_episode_number = video.season_episode_number if has_external else None
        video.episode_number_source = (
            "manual" if is_manual else "derived_from_part_offset" if offset is not None
            else "filename"
        )
        video.episode_number_confidence = 1.0 if is_manual else 0.9 if offset is not None else 0.95


def recalculate_collection_numbering(
    collection: CatalogCollection, videos_by_title: dict[int, list[Video]]
) -> None:
    preceding = 0
    preceding_known = True
    for title in sorted(
        collection.titles,
        key=lambda value: (
            value.effective_sort_order,
            value.part_number or value.effective_season_number or 0,
        ),
    ):
        known = preceding if preceding_known and preceding else None
        recalculate_title_numbering(
            title, videos_by_title.get(title.id, []), known_preceding_episodes=known
        )
        official_count = title.metadata_record.episode_count if title.metadata_record else None
        if official_count is not None:
            preceding += official_count
        else:
            preceding_known = False


def set_title_numbering(
    title: CatalogTitle, mode: str, offset: int | None
) -> None:
    if mode not in NUMBERING_MODES:
        raise ValueError("Neplatný režim číslování.")
    if offset is not None and offset < 0:
        raise ValueError("Offset nesmí být záporný.")
    title.numbering_mode = mode
    title.episode_start_offset = offset
    title.numbering_manual = mode != "unknown" or offset is not None
    title.numbering_verified_at = (
        datetime.now(timezone.utc) if title.numbering_manual else None
    )


def set_video_episode_override(video: Video, value: int | None) -> None:
    if value is not None and value <= 0:
        raise ValueError("Číslo epizody musí být kladné.")
    video.episode_number_manual_override = value
    video.episode_number_verified_at = datetime.now(timezone.utc) if value else None


def deterministic_video_order(videos: list[Video]) -> list[Video]:
    """Return stable filename-first ordering for explicit sequential numbering."""
    return sorted(
        videos,
        key=lambda video: (
            natural_sort_key(video.filename),
            natural_sort_key(video.relative_path),
            video.id or 0,
        ),
    )


def preview_sequential_numbering(
    videos: list[Video], start_episode: int
) -> list[SequentialNumberingRow]:
    if start_episode <= 0:
        raise ValueError("Počáteční číslo epizody musí být kladné.")
    rows = []
    for index, video in enumerate(deterministic_video_order(videos)):
        proposed = start_episode + index
        current = video.season_episode_number
        rows.append(SequentialNumberingRow(
            video_id=video.id,
            filename=video.filename,
            current_episode=current,
            proposed_episode=proposed,
            manual_conflict=(
                video.episode_number_manual_override is not None
                and video.episode_number_manual_override != proposed
            ),
        ))
    return rows


def apply_sequential_numbering(
    videos: list[Video], start_episode: int, *, confirm_manual_conflicts: bool = False
) -> list[SequentialNumberingRow]:
    rows = preview_sequential_numbering(videos, start_episode)
    if any(row.manual_conflict for row in rows) and not confirm_manual_conflicts:
        raise ValueError(
            "Náhled je v konfliktu s ručně zadanými čísly; jejich přepsání je nutné "
            "explicitně potvrdit."
        )
    videos_by_id = {video.id: video for video in videos}
    for row in rows:
        video = videos_by_id[row.video_id]
        if video.episode_number_manual_override != row.proposed_episode:
            set_video_episode_override(video, row.proposed_episode)
    return rows


SUPPLEMENTAL_PART_TYPES = {
    "film", "ova", "special", "preview", "recap", "bonus", "other",
}


def summarize_title_numbering(
    videos: list[Video], title: CatalogTitle | None = None,
) -> TitleNumberingSummary:
    supplemental = bool(
        title is not None and title.effective_part_type in SUPPLEMENTAL_PART_TYPES
    )
    detections = [
        detect_episode_number(video.filename) if video.episode_number_manual_override is None
        else None
        for video in videos
    ]
    standard_total = sum(
        detection is None or detection.is_standard for detection in detections
    )
    nonstandard = sum(
        detection is not None and detection.is_nonstandard for detection in detections
    )
    unknown = sum(
        video.season_episode_number is None
        and (detection is None or not detection.is_nonstandard)
        for video, detection in zip(videos, detections)
    )
    values = [
        video.season_episode_number
        for video in videos
        if video.season_episode_number is not None
    ]
    unique_values = set(values)
    episode_min = min(unique_values) if unique_values else None
    episode_max = max(unique_values) if unique_values else None
    gaps = tuple(
        sorted(set(range(episode_min, episode_max + 1)) - unique_values)
    ) if episode_min is not None and episode_max is not None else ()
    duplicates = tuple(sorted(
        value for value, count in Counter(values).items() if count > 1
    ))
    return TitleNumberingSummary(
        total=len(videos),
        standard_total=standard_total,
        numbered=len(values),
        unknown=unknown,
        nonstandard=nonstandard,
        episode_min=episode_min,
        episode_max=episode_max,
        gaps=gaps,
        duplicate_numbers=duplicates,
        supplemental=supplemental,
    )


def collection_requires_numbering_review(collection: CatalogCollection) -> bool:
    return any(
        summarize_title_numbering(list(title.videos), title).requires_review
        for title in collection.titles
    )
