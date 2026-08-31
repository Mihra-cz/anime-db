from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import CatalogTitle, Video, VideoVariantGroup
from .numbering import LogicalEpisodeIdentity, logical_episode_partitions


CONTENT_VARIANT_LABELS = {
    "censored": "Censored",
    "uncensored": "Uncensored",
    "other": "Other",
}


def video_variant_group_display(group: VideoVariantGroup | None) -> str:
    """Return the compact manual-authority label used by the public catalog."""
    if group is None:
        return "Varianta neurčena"
    content = CONTENT_VARIANT_LABELS.get(
        (group.content_variant or "").casefold(), group.content_variant or ""
    )
    return " · ".join(value for value in (group.manual_label, content) if value)


def video_variant_group_technical_details(
    group: VideoVariantGroup | None,
) -> tuple[str, ...]:
    if group is None:
        return ()
    details = []
    if group.release_source:
        details.append(f"Zdroj {group.release_source.upper()}")
    if group.content_variant:
        details.append(
            CONTENT_VARIANT_LABELS.get(
                group.content_variant.casefold(), group.content_variant
            )
        )
    return tuple(details)


def _loaded_variant_group(video: Video) -> VideoVariantGroup | None:
    """Read an eagerly loaded/manual in-memory group without causing lazy SQL."""
    return video.__dict__.get("video_variant_group")


def video_variant_display_for_video(
    video: Video, *, include_unassigned: bool = False,
) -> str | None:
    group = _loaded_variant_group(video)
    if group is None and not include_unassigned:
        return None
    return video_variant_group_display(group)


@dataclass(frozen=True)
class PhysicalVideoPresentation:
    video: Video
    duplicate_copies: tuple[Video, ...] = ()
    orphan_duplicate: bool = False


@dataclass(frozen=True)
class VariantLanePresentation:
    group_id: int | None
    label: str
    technical_details: tuple[str, ...]
    physical_rows: tuple[PhysicalVideoPresentation, ...]
    unresolved_duplicate_candidate: bool = False


@dataclass(frozen=True)
class LogicalEpisodePresentation:
    identity: LogicalEpisodeIdentity
    label: str
    lanes: tuple[VariantLanePresentation, ...]
    show_variant_lanes: bool
    unresolved_variant_ambiguity: bool = False


@dataclass(frozen=True)
class PresentedVideoRow:
    physical: PhysicalVideoPresentation
    episode_heading: LogicalEpisodePresentation | None = None
    variant_heading: VariantLanePresentation | None = None
    compact_variant_label: str | None = None

    @property
    def video(self) -> Video:
        return self.physical.video

    @property
    def duplicate_copies(self) -> tuple[Video, ...]:
        return self.physical.duplicate_copies

    @property
    def orphan_duplicate(self) -> bool:
        return self.physical.orphan_duplicate


@dataclass(frozen=True)
class CatalogTitleVideoPresentation:
    logical_episodes: tuple[LogicalEpisodePresentation, ...]
    other_physical_rows: tuple[PhysicalVideoPresentation, ...]
    display_rows: tuple[PresentedVideoRow, ...]


def physical_video_rows(
    videos: Iterable[Video], known_video_ids: set[int] | None = None,
) -> tuple[PhysicalVideoPresentation, ...]:
    ordered = tuple(videos)
    included_ids = {video.id for video in ordered if video.id is not None}
    available_ids = included_ids if known_video_ids is None else known_video_ids
    duplicate_copies_by_primary: dict[int, list[Video]] = {}
    for video in ordered:
        if video.duplicate_of_video_id in included_ids:
            duplicate_copies_by_primary.setdefault(
                video.duplicate_of_video_id, []
            ).append(video)
    return tuple(
        PhysicalVideoPresentation(
            video=video,
            duplicate_copies=tuple(duplicate_copies_by_primary.get(video.id, ())),
            orphan_duplicate=bool(
                video.duplicate_primary_missing
                or video.duplicate_of_video_id is not None
                and video.duplicate_of_video_id not in available_ids
            ),
        )
        for video in ordered
        if video.duplicate_of_video_id not in included_ids
    )


def build_catalog_title_video_presentation(
    visible_videos: Iterable[Video],
    catalog_title: CatalogTitle,
    *,
    known_videos: Iterable[Video] | None = None,
) -> CatalogTitleVideoPresentation:
    """Build the read-only logical episode → variant → physical hierarchy.

    Logical identities and active variant partitions come exclusively from the
    shared numbering layer. Confirmed duplicate secondaries are attached below
    their active primary and never create another episode or variant lane.
    """
    visible = tuple(visible_videos)
    known = tuple(known_videos) if known_videos is not None else visible
    visible_ids = {video.id for video in visible if video.id is not None}
    known_ids = {video.id for video in known if video.id is not None}
    visible_order = {
        video.id: index for index, video in enumerate(visible) if video.id is not None
    }
    known_duplicates_by_primary: dict[int, list[Video]] = {}
    for video in known:
        if video.duplicate_of_video_id is not None:
            known_duplicates_by_primary.setdefault(
                video.duplicate_of_video_id, []
            ).append(video)

    episodes_with_order = []
    consumed_visible_ids: set[int] = set()
    for partition in logical_episode_partitions(
        list(known), catalog_title=catalog_title
    ):
        unresolved_sets = tuple(
            {video.id for video in items if video.id is not None}
            for items in partition.unresolved_video_groups
        )
        lane_specs = [
            (
                variant.video_variant_group_id,
                tuple(variant.videos),
                _loaded_variant_group(variant.videos[0]) if variant.videos else None,
            )
            for variant in partition.confirmed_variants
        ]
        if partition.unassigned_videos:
            lane_specs.append((None, partition.unassigned_videos, None))

        lanes_with_order = []
        partition_primary_ids = {
            video.id for video in partition.videos if video.id is not None
        }
        has_confirmed_copies = any(
            known_duplicates_by_primary.get(video_id)
            for video_id in partition_primary_ids
        )
        show_variant_lanes = bool(
            len(partition.videos) > 1
            or partition.confirmed_variants
            or has_confirmed_copies
        )
        for group_id, primaries, group in lane_specs:
            primary_ids = {video.id for video in primaries if video.id is not None}
            lane_member_ids = set(primary_ids)
            for primary_id in primary_ids:
                lane_member_ids.update(
                    duplicate.id
                    for duplicate in known_duplicates_by_primary.get(primary_id, ())
                    if duplicate.id is not None
                )
            selected_members = tuple(
                video for video in visible if video.id in lane_member_ids
            )
            if not selected_members:
                continue
            consumed_visible_ids.update(
                video.id for video in selected_members if video.id is not None
            )
            physical_rows = physical_video_rows(selected_members, known_ids)
            if not physical_rows:
                continue
            lane = VariantLanePresentation(
                group_id=group_id,
                label=video_variant_group_display(group),
                technical_details=video_variant_group_technical_details(group),
                physical_rows=physical_rows,
                unresolved_duplicate_candidate=any(
                    primary_ids & unresolved_ids for unresolved_ids in unresolved_sets
                ),
            )
            order = min(
                visible_order.get(row.video.id, len(visible)) for row in physical_rows
            )
            lanes_with_order.append((order, lane))
        if not lanes_with_order:
            continue
        lanes = tuple(lane for _order, lane in sorted(
            lanes_with_order, key=lambda item: (item[0], item[1].label.casefold())
        ))
        episode = LogicalEpisodePresentation(
            identity=partition.identity,
            label=f"E{partition.identity.season_episode_number:02d}",
            lanes=lanes,
            show_variant_lanes=show_variant_lanes,
            unresolved_variant_ambiguity=bool(partition.unresolved_video_groups),
        )
        episodes_with_order.append((min(order for order, _lane in lanes_with_order), episode))

    logical_episodes = tuple(
        episode for _order, episode in sorted(
            episodes_with_order,
            key=lambda item: (item[0], item[1].identity.season_episode_number),
        )
    )
    other_visible = tuple(
        video for video in visible if video.id not in consumed_visible_ids
    )
    other_rows = physical_video_rows(other_visible, known_ids)

    display_rows = []
    for episode in logical_episodes:
        first_episode_row = True
        for lane in episode.lanes:
            first_lane_row = True
            for physical in lane.physical_rows:
                display_rows.append(PresentedVideoRow(
                    physical=physical,
                    episode_heading=(
                        episode
                        if episode.show_variant_lanes and first_episode_row else None
                    ),
                    variant_heading=(
                        lane if episode.show_variant_lanes and first_lane_row else None
                    ),
                ))
                first_episode_row = False
                first_lane_row = False
    for physical in other_rows:
        group = _loaded_variant_group(physical.video)
        display_rows.append(PresentedVideoRow(
            physical=physical,
            compact_variant_label=(
                video_variant_group_display(group) if group is not None else None
            ),
        ))
    return CatalogTitleVideoPresentation(
        logical_episodes=logical_episodes,
        other_physical_rows=other_rows,
        display_rows=tuple(display_rows),
    )


def ungrouped_presented_video_rows(
    videos: Iterable[Video], known_video_ids: set[int] | None = None,
) -> tuple[PresentedVideoRow, ...]:
    """Compatibility presentation for detached/direct template rendering."""
    return tuple(
        PresentedVideoRow(
            physical=row,
            compact_variant_label=(
                video_variant_group_display(group)
                if (group := _loaded_variant_group(row.video)) is not None else None
            ),
        )
        for row in physical_video_rows(videos, known_video_ids)
    )
