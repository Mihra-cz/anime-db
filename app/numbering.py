from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from .catalog import (
    EpisodeNumberDetection, detect_episode_number, natural_sort_key, normalize_title,
)
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
    resolved_supplemental: int
    episode_min: int | None
    episode_max: int | None
    gaps: tuple[int, ...]
    duplicate_numbers: tuple[int, ...]
    confirmed_duplicates: int
    invalid_duplicate_references: int

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


@dataclass(frozen=True)
class EpisodeDuplicateGroup:
    episode_number: int
    videos: tuple[Video, ...]
    primary: Video | None = None
    supplementary_type: str | None = None
    context_label: str | None = None

    @property
    def display_label(self) -> str:
        if self.supplementary_type:
            label = {
                "ova": "OVA", "special": "Special", "ncop": "NCOP", "nced": "NCED",
                "op": "OP", "ed": "ED", "preview": "Preview", "recap": "Recap",
                "bonus": "Bonus",
            }.get(self.supplementary_type, self.supplementary_type.upper())
            context = f" · {self.context_label}" if self.context_label else ""
            return f"{label} {self.episode_number:02d}{context}"
        return f"E{self.episode_number:02d}"

    @property
    def duplicate_copies(self) -> tuple[Video, ...]:
        return tuple(video for video in self.videos if video is not self.primary)


def is_confirmed_duplicate(video: Video) -> bool:
    return video.duplicate_of_video_id is not None or video.duplicate_of is not None


def is_nonprimary_duplicate_video(video: Video) -> bool:
    return is_confirmed_duplicate(video) or video.duplicate_primary_missing


@dataclass(frozen=True)
class VideoNumberingIdentity:
    kind: str
    number: int
    supplementary_type: str | None = None
    context_key: str | None = None
    context_label: str | None = None
    context_season_number: int | None = None


def _normalized_context_name(value: str) -> str:
    return normalize_title(value.rsplit("(", 1)[0].strip())


def supplementary_context_map(videos: list[Video]) -> dict[str, list[CatalogTitle]]:
    titles: dict[int, CatalogTitle] = {}
    for video in videos:
        collection = (
            video.catalog_title.collection if video.catalog_title is not None
            else video.catalog_collection
        )
        if collection is not None:
            titles.update({title.id: title for title in collection.titles})
    by_name: dict[str, list[CatalogTitle]] = {}
    for title in titles.values():
        for name in {
            normalize_title(title.local_title), _normalized_context_name(title.local_title),
            normalize_title(title.manual_display_title or ""),
        } - {""}:
            by_name.setdefault(name, []).append(title)
    return by_name


def video_numbering_identity(
    video: Video, *, title_names: dict[str, list[CatalogTitle]] | None = None,
) -> VideoNumberingIdentity | None:
    detection = detect_episode_number(video.filename)
    if detection.is_supplementary and detection.supplementary_number is not None:
        title_names = title_names or supplementary_context_map([video])
        hint = normalize_title(detection.context_hint or "")
        matched = title_names.get(hint, []) if hint else []
        current_title = video.catalog_title
        current_collection = (
            current_title.collection if current_title is not None
            else video.catalog_collection
        )
        collection_names = {
            normalize_title(current_collection.local_title),
            normalize_title(current_collection.manual_display_title or ""),
        } - {""} if current_collection is not None else set()
        if len(matched) == 1:
            context_title = matched[0]
            context_key = f"title:{context_title.id}"
            context_label = context_title.effective_season_label or context_title.local_title
            context_season_number = context_title.effective_season_number
        elif (
            hint and hint in collection_names and current_title is not None
            and (
                current_title.effective_season_number is not None
                or current_title.effective_season_label
            )
        ):
            context_key = f"title:{current_title.id}"
            context_label = (
                current_title.effective_season_label
                or f"S{current_title.effective_season_number}"
            )
            context_season_number = current_title.effective_season_number
        elif hint:
            context_key, context_label = f"name:{hint}", detection.context_hint
            context_season_number = None
        else:
            context_title = current_title
            if context_title is not None and (
                context_title.effective_season_number is not None
                or context_title.effective_season_label
            ):
                context_key = f"title:{context_title.id}"
                context_label = (
                    context_title.effective_season_label
                    or f"S{context_title.effective_season_number}"
                )
                context_season_number = context_title.effective_season_number
            else:
                parent = str(video.relative_path.rsplit("/", 1)[0])
                parent_name = parent.rsplit("/", 1)[-1]
                context_key = f"path:{normalize_title(parent)}"
                context_label = (
                    parent_name
                    if normalize_title(parent_name) not in {
                        "nc", "ncop", "nced", "op", "ed", "ova", "oad",
                        "special", "specials", "bonus", "bonuses", "extras",
                    }
                    else None
                )
                context_season_number = None
        return VideoNumberingIdentity(
            "supplementary", detection.supplementary_number,
            detection.supplementary_type, context_key, context_label,
            context_season_number,
        )
    if video.season_episode_number is not None and not video.content_type_manual:
        return VideoNumberingIdentity("standard", video.season_episode_number)
    return None


def unresolved_duplicate_groups(videos: list[Video]) -> tuple[EpisodeDuplicateGroup, ...]:
    by_identity: dict[VideoNumberingIdentity, list[Video]] = {}
    title_names = supplementary_context_map(videos)
    for video in videos:
        identity = video_numbering_identity(video, title_names=title_names)
        if identity is not None and not is_confirmed_duplicate(video):
            by_identity.setdefault(identity, []).append(video)
    return tuple(
        EpisodeDuplicateGroup(
            identity.number, tuple(sorted(items, key=deterministic_video_order_key)),
            supplementary_type=identity.supplementary_type,
            context_label=identity.context_label,
        )
        for identity, items in sorted(
            by_identity.items(),
            key=lambda item: (
                item[0].kind, item[0].supplementary_type or "",
                item[0].context_key or "", item[0].number,
            ),
        ) if len(items) > 1
    )


def confirmed_duplicate_groups(videos: list[Video]) -> tuple[EpisodeDuplicateGroup, ...]:
    videos_by_id = {video.id: video for video in videos if video.id is not None}
    title_names = supplementary_context_map(videos)
    copies_by_primary: dict[Video, list[Video]] = {}
    for video in videos:
        primary = video.duplicate_of
        if primary is None and video.duplicate_of_video_id is not None:
            primary = videos_by_id.get(video.duplicate_of_video_id)
        if primary is not None:
            copies_by_primary.setdefault(primary, []).append(video)
    groups = []
    for primary, copies in copies_by_primary.items():
        members = tuple(sorted([primary, *copies], key=deterministic_video_order_key))
        identity = video_numbering_identity(primary, title_names=title_names)
        groups.append(EpisodeDuplicateGroup(
            identity.number if identity else primary.season_episode_number or 0,
            members, primary=primary,
            supplementary_type=identity.supplementary_type if identity else None,
            context_label=identity.context_label if identity else None,
        ))
    return tuple(sorted(groups, key=lambda group: (group.episode_number, group.primary.id or 0)))


def set_duplicate_group_primary(videos: list[Video], primary: Video) -> None:
    if len(videos) != len(set(videos)):
        raise ValueError("Skupina duplicity nesmí obsahovat stejné video vícekrát.")
    members = _complete_duplicate_group(videos)
    if len(members) < 2 or primary not in members:
        raise ValueError("Skupina duplicity musí obsahovat primární video a alespoň jednu kopii.")
    title_ids = {video.catalog_title_id for video in members}
    collection_ids = {video.catalog_collection_id for video in members}
    title_names = supplementary_context_map(list(members))
    identities = {
        video_numbering_identity(video, title_names=title_names) for video in members
    }
    if len(title_ids) != 1 or None in title_ids or len(collection_ids) != 1:
        raise ValueError("Duplicitní videa musí patřit do stejné části a kolekce.")
    if len(identities) != 1 or None in identities:
        raise ValueError("Duplicitní videa musí mít stejnou známou logickou identitu.")
    for video in members:
        video.duplicate_of = None
        video.duplicate_primary_missing = False
    for video in members:
        if video is not primary:
            video.duplicate_of = primary


def clear_duplicate_group(videos: list[Video]) -> None:
    for video in _complete_duplicate_group(videos):
        video.duplicate_of = None
        video.duplicate_primary_missing = False


def _complete_duplicate_group(videos: list[Video]) -> set[Video]:
    members = set(videos)
    pending = list(videos)
    while pending:
        video = pending.pop()
        related = [*video.duplicate_copies]
        if video.duplicate_of is not None:
            related.append(video.duplicate_of)
        for candidate in related:
            if candidate not in members:
                members.add(candidate)
                pending.append(candidate)
    return members


def recalculate_title_numbering(
    title: CatalogTitle,
    videos: list[Video],
    *,
    known_preceding_episodes: int | None = None,
    external_linked: bool | None = None,
) -> None:
    detections = [detect_episode_number(video.filename) for video in videos]
    title_is_supplemental = title.effective_part_type in SUPPLEMENTAL_PART_TYPES
    detected = [
        item.number if item.is_standard and not title_is_supplemental else None
        for item in detections
    ]
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
            video.episode_number_source = (
                f"supplementary_{detection.supplementary_type}"
                if detection.is_supplementary else {
                "zero": "nonstandard_zero",
                "fractional": "fractional",
                }.get(detection.kind, "unknown")
            )
            video.episode_number_confidence = (
                0.95 if detection.is_nonstandard or detection.is_supplementary else None
            )
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
            else "sxxexx" if detection.season_hint is not None else "filename"
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
        key=deterministic_video_order_key,
    )


def deterministic_video_order_key(video: Video):
    return (
        natural_sort_key(video.filename),
        natural_sort_key(video.relative_path),
        video.id or 0,
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


@dataclass(frozen=True)
class EffectiveVideoNumbering:
    """Aktivní interpretace videa; raw filename detekce zůstává dostupná pro audit."""

    detection: EpisodeNumberDetection
    classification: str
    season_episode_number: int | None
    numbering_input: int | None
    manual_override: bool

    @property
    def is_standard(self) -> bool:
        return self.classification == "standard"

    @property
    def is_supplementary(self) -> bool:
        return self.classification == "supplementary"

    @property
    def is_nonstandard(self) -> bool:
        return self.classification == "nonstandard"

    @property
    def is_unknown(self) -> bool:
        return self.classification == "unknown"


def effective_video_numbering(
    video: Video, title: CatalogTitle | None = None,
) -> EffectiveVideoNumbering:
    """Sjednotí manual/content/title autoritu nad automatickým filename parserem."""
    detection = detect_episode_number(video.filename)
    effective_title = title if title is not None else video.catalog_title
    title_is_supplemental = bool(
        effective_title is not None
        and effective_title.effective_part_type in SUPPLEMENTAL_PART_TYPES
    )
    if video.content_type_manual or title_is_supplemental:
        classification = "supplementary"
    elif video.episode_number_manual_override is not None:
        classification = "standard"
    elif detection.is_supplementary:
        classification = "supplementary"
    elif detection.is_nonstandard:
        classification = "nonstandard"
    elif video.season_episode_number is not None or detection.is_standard:
        classification = "standard"
    else:
        classification = "unknown"
    numbering_input = (
        video.episode_number_manual_override
        if video.episode_number_manual_override is not None
        else video.local_episode_number
        if video.local_episode_number is not None
        else detection.number if detection.is_standard else None
    )
    return EffectiveVideoNumbering(
        detection=detection,
        classification=classification,
        season_episode_number=video.season_episode_number,
        numbering_input=numbering_input,
        manual_override=video.episode_number_manual_override is not None,
    )


def summarize_title_numbering(
    videos: list[Video], title: CatalogTitle | None = None,
) -> TitleNumberingSummary:
    supplemental = bool(
        title is not None and title.effective_part_type in SUPPLEMENTAL_PART_TYPES
    )
    states = [effective_video_numbering(video, title) for video in videos]
    confirmed_duplicate = [is_nonprimary_duplicate_video(video) for video in videos]
    standard_total = 0 if supplemental else sum(
        state.is_standard and not is_duplicate
        for state, is_duplicate in zip(states, confirmed_duplicate)
    )
    nonstandard = 0 if supplemental else sum(
        state.is_nonstandard and not is_duplicate
        for state, is_duplicate in zip(states, confirmed_duplicate)
    )
    unknown = 0 if supplemental else sum(
        (
            state.is_unknown
            or state.is_standard and state.season_episode_number is None
        ) and not is_duplicate
        for state, is_duplicate in zip(states, confirmed_duplicate)
    )
    values = [] if supplemental else [
        state.season_episode_number
        for video, state in zip(videos, states)
        if state.is_standard
        and state.season_episode_number is not None
        and not is_nonprimary_duplicate_video(video)
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
        resolved_supplemental=(
            len(videos) if supplemental else sum(state.is_supplementary for state in states)
        ),
        episode_min=episode_min,
        episode_max=episode_max,
        gaps=gaps,
        duplicate_numbers=duplicates,
        confirmed_duplicates=sum(is_confirmed_duplicate(video) for video in videos),
        invalid_duplicate_references=sum(
            bool(video.duplicate_primary_missing) for video in videos
        ),
        supplemental=supplemental,
    )


def collection_requires_numbering_review(collection: CatalogCollection) -> bool:
    return any(
        summarize_title_numbering(list(title.videos), title).requires_review
        for title in collection.titles
    )
