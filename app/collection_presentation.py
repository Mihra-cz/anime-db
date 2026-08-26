from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .catalog import sort_title_videos
from .hierarchy_types import (
    MAIN_CONTENT_PART_TYPES,
    PART_TYPE_LABELS,
    SUPPLEMENTARY_PART_TYPES,
)
from .models import CatalogTitle, Video
from .title_order import catalog_title_sort_key


@dataclass(frozen=True)
class TitlePresentation:
    title: CatalogTitle
    videos: tuple[Video, ...]


@dataclass(frozen=True)
class SupplementaryGroupPresentation:
    part_type: str
    label: str
    parts: tuple[TitlePresentation, ...]

    @property
    def video_count(self) -> int:
        return sum(len(part.videos) for part in self.parts)


@dataclass(frozen=True)
class PrimaryTitlePresentation:
    title: CatalogTitle
    supplementary_groups: tuple[SupplementaryGroupPresentation, ...]

    @property
    def supplementary_parts(self) -> tuple[TitlePresentation, ...]:
        return tuple(
            part
            for group in self.supplementary_groups
            for part in group.parts
        )

    @property
    def supplementary_video_count(self) -> int:
        return sum(group.video_count for group in self.supplementary_groups)

    @property
    def supplementary_video_counts_by_type(
        self,
    ) -> tuple[SupplementaryGroupPresentation, ...]:
        """Non-empty attached groups used by the season overview tooltip."""
        return tuple(
            group for group in self.supplementary_groups if group.video_count
        )

    @property
    def supplementary_video_tooltip(self) -> str:
        return "\n".join(
            f"{group.label}: {group.video_count}"
            for group in self.supplementary_video_counts_by_type
        )


@dataclass(frozen=True)
class CollectionPresentation:
    """Read-only main-UI projection over the persisted CatalogTitle hierarchy."""

    primary_parts: tuple[PrimaryTitlePresentation, ...]
    anime_level_parts: tuple[TitlePresentation, ...]

    @property
    def direct_title(self) -> CatalogTitle | None:
        """Keep direct navigation only when no anime-level sibling would vanish."""
        if len(self.primary_parts) == 1 and not self.anime_level_parts:
            return self.primary_parts[0].title
        if not self.primary_parts and len(self.anime_level_parts) == 1:
            return self.anime_level_parts[0].title
        return None

    @property
    def all_title_ids(self) -> frozenset[int]:
        return frozenset(
            title_id
            for title_id in (
                *(part.title.id for part in self.primary_parts),
                *(
                    item.title.id
                    for part in self.primary_parts
                    for item in part.supplementary_parts
                ),
                *(part.title.id for part in self.anime_level_parts),
            )
            if title_id is not None
        )

    def primary_part_for_title(
        self, catalog_title_id: int | None,
    ) -> PrimaryTitlePresentation | None:
        return next(
            (
                part for part in self.primary_parts
                if part.title.id == catalog_title_id
            ),
            None,
        )


def _title_presentation(title: CatalogTitle) -> TitlePresentation:
    videos, _, _ = sort_title_videos(title.videos)
    return TitlePresentation(title=title, videos=tuple(videos))


def _supplementary_groups(
    titles: Iterable[CatalogTitle],
) -> tuple[SupplementaryGroupPresentation, ...]:
    grouped: dict[str, list[TitlePresentation]] = {}
    for title in titles:
        part_type = title.effective_part_type
        grouped.setdefault(part_type, []).append(_title_presentation(title))
    return tuple(
        SupplementaryGroupPresentation(
            part_type=part_type,
            label=PART_TYPE_LABELS.get(
                part_type, part_type.replace("_", " ").title(),
            ),
            parts=tuple(parts),
        )
        for part_type, parts in grouped.items()
    )


def build_collection_presentation(
    titles: Iterable[CatalogTitle],
) -> CollectionPresentation:
    """Nest only supplementary titles with one exact persisted season match.

    A title without a numeric effective season context, an unknown structural
    type, a missing matching primary title, or an ambiguous match remains at
    anime level.  The function only reads ORM state and uses the central title
    ordering key; it never infers hierarchy from filenames or metadata.
    """
    ordered = tuple(sorted(titles, key=catalog_title_sort_key))
    primary_titles = tuple(
        title
        for title in ordered
        if title.effective_part_type in MAIN_CONTENT_PART_TYPES
    )
    primary_by_season: dict[int, list[CatalogTitle]] = defaultdict(list)
    for title in primary_titles:
        if title.effective_season_number is not None:
            primary_by_season[title.effective_season_number].append(title)

    attached: dict[int, list[CatalogTitle]] = defaultdict(list)
    anime_level: list[CatalogTitle] = []
    for title in ordered:
        if title in primary_titles:
            continue
        season_number = title.effective_season_number
        matching_primary = (
            primary_by_season.get(season_number, [])
            if season_number is not None
            else []
        )
        if (
            title.effective_part_type in SUPPLEMENTARY_PART_TYPES
            and len(matching_primary) == 1
        ):
            attached[id(matching_primary[0])].append(title)
            continue
        anime_level.append(title)

    return CollectionPresentation(
        primary_parts=tuple(
            PrimaryTitlePresentation(
                title=title,
                supplementary_groups=_supplementary_groups(
                    attached.get(id(title), ()),
                ),
            )
            for title in primary_titles
        ),
        anime_level_parts=tuple(
            _title_presentation(title) for title in anime_level
        ),
    )
