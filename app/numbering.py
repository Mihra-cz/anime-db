from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re

from sqlalchemy.orm import Session

from .catalog import (
    EpisodeNumberDetection, FILE_TYPE_TO_SUPPLEMENTARY_SUBTYPE,
    detect_episode_number, effective_video_content_type, natural_sort_key,
    normalize_title,
)
from .hierarchy_authority import manual_hierarchy_snapshot_uses_legacy_projection
from .models import CatalogCollection, CatalogTitle, Video, utc_now

NUMBERING_MODES = {"unknown", "season_local", "absolute", "mixed"}


@dataclass(frozen=True)
class SequentialNumberingRow:
    video_id: int
    filename: str
    current_episode: int | None
    proposed_episode: int
    manual_conflict: bool


@dataclass
class BulkRenumberMetrics:
    """Operation counts for the title-scoped linear proposal pass."""

    logical_episodes_scanned: int = 0
    physical_videos_scanned: int = 0


@dataclass(frozen=True)
class BulkRenumberPhysicalChange:
    video_id: int
    filename: str
    current_episode: int
    proposed_episode: int
    manual_override: bool
    confirmed_duplicate_secondary: bool
    video_variant_group_id: int | None


@dataclass(frozen=True)
class BulkRenumberLogicalChange:
    current_episode: int
    proposed_episode: int
    physical_changes: tuple[BulkRenumberPhysicalChange, ...]

    @property
    def has_manual_override(self) -> bool:
        return any(change.manual_override for change in self.physical_changes)


@dataclass(frozen=True)
class DeterministicBulkRenumberProposal:
    catalog_title_id: int
    title_name: str
    gap_start: int
    gap_end: int
    offset: int
    recap_positions: tuple[str, ...]
    rows: tuple[BulkRenumberLogicalChange, ...]
    expected_episode_count: int | None
    expected_count_authoritative: bool
    warnings: tuple[str, ...]
    fingerprint: str

    @property
    def logical_episode_count(self) -> int:
        return len(self.rows)

    @property
    def physical_video_count(self) -> int:
        return sum(len(row.physical_changes) for row in self.rows)

    @property
    def has_manual_overrides(self) -> bool:
        return any(row.has_manual_override for row in self.rows)

    @property
    def gap_label(self) -> str:
        if self.gap_start == self.gap_end:
            return f"E{self.gap_start}"
        return f"E{self.gap_start}–E{self.gap_end}"

    @property
    def suffix_label(self) -> str:
        if len(self.rows) == 1:
            return f"E{self.rows[0].current_episode}"
        return f"E{self.rows[0].current_episode}–E{self.rows[-1].current_episode}"


@dataclass(frozen=True)
class TitleNumberingSummary:
    total: int
    standard_total: int
    numbered: int
    unnumbered_standard: int
    confirmed_variant_instance_count: int
    unassigned_variant_video_count: int
    unknown: int
    nonstandard: int
    resolved_supplemental: int
    episode_min: int | None
    episode_max: int | None
    gaps: tuple[int, ...]
    duplicate_numbers: tuple[int, ...]
    confirmed_duplicates: int
    invalid_duplicate_references: int
    variant_inconsistent_confirmed_duplicates: int

    supplemental: bool = False

    @property
    def physical_video_count(self) -> int:
        return self.total

    @property
    def logical_episode_count(self) -> int:
        return self.standard_total

    @property
    def confirmed_duplicate_count(self) -> int:
        return self.confirmed_duplicates

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
    episode_number: int | Decimal
    videos: tuple[Video, ...]
    primary: Video | None = None
    supplementary_type: str | None = None
    context_label: str | None = None
    video_variant_group_id: int | None = None
    video_variant_label: str | None = None
    has_unassigned_variant: bool = False

    @property
    def display_label(self) -> str:
        if self.supplementary_type:
            label = {
                "ova": "OVA", "special": "Special", "ncop": "NCOP", "nced": "NCED",
                "op": "OP", "ed": "ED", "preview": "Preview", "recap": "Recap",
                "bonus": "Bonus", "cm": "CM", "menu": "Menu",
            }.get(self.supplementary_type, self.supplementary_type.upper())
            context = f" · {self.context_label}" if self.context_label else ""
            decimal_number = (
                self.episode_number
                if isinstance(self.episode_number, Decimal)
                else Decimal(self.episode_number)
            )
            position = (
                f"{int(decimal_number):02d}"
                if decimal_number == decimal_number.to_integral_value()
                else format_episode_position(decimal_number)
            )
            return f"{label} {position}{context}"
        variant = (
            f" · varianta {self.video_variant_label}"
            if self.video_variant_label else ""
        )
        return f"E{self.episode_number:02d}{variant}"

    @property
    def duplicate_copies(self) -> tuple[Video, ...]:
        return tuple(video for video in self.videos if video is not self.primary)


def is_confirmed_duplicate(video: Video) -> bool:
    return video.duplicate_of_video_id is not None or video.duplicate_of is not None


def is_nonprimary_duplicate_video(video: Video) -> bool:
    return is_confirmed_duplicate(video) or video.duplicate_primary_missing


RECAP_NUMBER_INPUT = re.compile(r"^(?P<base>[1-9]\d*)(?:\.(?P<digit>\d))?$")
RECAP_NUMBER_ERROR = (
    "Recap číslo musí být kladné celé číslo nebo hodnota s právě jedním "
    "desetinným místem, například 14.5 nebo 24.9."
)
STANDARD_NUMBER_ERROR = "Standardní ruční číslo epizody musí být kladné celé číslo."


def format_episode_position(value: int | Decimal) -> str:
    """Format an exact canonical/supplementary position without float conversion."""
    decimal_value = value if isinstance(value, Decimal) else Decimal(value)
    if decimal_value == decimal_value.to_integral_value():
        return str(int(decimal_value))
    return format(decimal_value, ".1f")


def manual_recap_episode_number(video: Video) -> Decimal | None:
    """Return explicit Recap authority, including a legacy integer fallback."""
    tenths = video.recap_episode_number_manual_tenths
    if tenths is not None:
        return Decimal(tenths) / Decimal(10)
    if video.episode_number_manual_override is None:
        return None
    manual_type = (video.content_type_manual or "").strip().casefold()
    loaded_title = video.__dict__.get("catalog_title")
    title_type = (
        loaded_title.effective_part_type if loaded_title is not None else None
    )
    if manual_type == "recap" or not manual_type and title_type == "recap":
        # Before fractional Recap support an integer entered through the same UI
        # lived in the canonical override column.  Read it as the old manual
        # Recap position until the user explicitly replaces or clears it.
        return Decimal(video.episode_number_manual_override)
    return None


def manual_episode_number_input_value(video: Video) -> str:
    if effective_video_content_type(video) == "recap":
        value = manual_recap_episode_number(video)
        return format_episode_position(value) if value is not None else ""
    value = video.episode_number_manual_override
    return str(value) if value is not None else ""


def _parse_recap_episode_tenths(raw_value: str) -> int | None:
    normalized = raw_value.strip()
    if not normalized:
        return None
    match = RECAP_NUMBER_INPUT.fullmatch(normalized)
    if match is None:
        raise ValueError(RECAP_NUMBER_ERROR)
    base = int(match.group("base"))
    digit = int(match.group("digit") or "0")
    return base * 10 + digit


def set_video_episode_number_from_input(video: Video, raw_value: str) -> None:
    """Validate the submitted form value against the effective content type."""
    if effective_video_content_type(video) == "recap":
        tenths = _parse_recap_episode_tenths(raw_value)
        video.recap_episode_number_manual_tenths = tenths
        # Replacing/clearing the one visible manual field explicitly retires the
        # legacy integer fallback.  It is never discarded by classification or
        # background recalculation.
        video.episode_number_manual_override = None
        video.episode_number_verified_at = utc_now() if tenths is not None else None
        return

    normalized = raw_value.strip()
    if normalized and not re.fullmatch(r"[1-9]\d*", normalized):
        raise ValueError(STANDARD_NUMBER_ERROR)
    value = int(normalized) if normalized else None
    set_video_episode_override(video, value)


def validate_recap_number_for_content_type(
    video: Video,
    content_type: str,
) -> None:
    """Prevent a Recap-only manual authority from becoming dormant/invalid."""
    if (
        content_type != "recap"
        and video.recap_episode_number_manual_tenths is not None
    ):
        raise ValueError(
            "Ruční Recap číslo je nutné před změnou typu výslovně odstranit "
            "nebo upravit v detailu videa; hodnota nebude smazána automaticky."
        )


@dataclass(frozen=True)
class SupplementaryNumberingHint:
    """Bezpečný automatický subtype a případné pořadí mimo canonical episodes."""

    supplementary_type: str
    number: int | Decimal | None


def automatic_supplementary_numbering(
    video: Video,
    detection: EpisodeNumberDetection | None = None,
) -> SupplementaryNumberingHint | None:
    """Spojí explicitní parser semantics s bezpečným scanner file type.

    Číslo z generic standard detekce je v tomto výsledku pouze supplementary
    ordinal/hint. Nestandardní structural/fractional/zero význam má před
    automatickým file type fallbackem přednost.
    """
    detection = detection or detect_episode_number(video.filename)
    if detection.is_supplementary and detection.supplementary_type:
        return SupplementaryNumberingHint(
            detection.supplementary_type,
            detection.supplementary_number,
        )
    if detection.is_nonstandard:
        return None
    supplementary_type = FILE_TYPE_TO_SUPPLEMENTARY_SUBTYPE.get(
        (video.file_type or "").strip().casefold()
    )
    if supplementary_type is None or not detection.is_standard:
        return None
    return SupplementaryNumberingHint(
        supplementary_type,
        detection.number,
    )


@dataclass(frozen=True)
class VideoNumberingIdentity:
    kind: str
    number: int | Decimal
    supplementary_type: str | None = None
    context_key: str | None = None
    context_label: str | None = None
    context_season_number: int | None = None


@dataclass(frozen=True)
class LogicalEpisodeIdentity:
    """One canonical standard episode inside exactly one CatalogTitle."""

    catalog_title_key: tuple[str, int]
    season_episode_number: int


@dataclass(frozen=True)
class ConfirmedVideoVariantPartition:
    video_variant_group_id: int
    videos: tuple[Video, ...]


@dataclass(frozen=True)
class LogicalEpisodePartition:
    """Active physical representations partitioned by manual variant authority."""

    identity: LogicalEpisodeIdentity
    videos: tuple[Video, ...]
    confirmed_variants: tuple[ConfirmedVideoVariantPartition, ...]
    unassigned_videos: tuple[Video, ...]

    @property
    def unresolved_video_groups(self) -> tuple[tuple[Video, ...], ...]:
        if self.unassigned_videos and len(self.videos) > 1:
            # A NULL assignment can still be a copy of any known lane or another
            # legitimate lane. Keep the whole collision visible for review.
            return (self.videos,)
        return tuple(
            variant.videos
            for variant in self.confirmed_variants
            if len(variant.videos) > 1
        )


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
    supplementary = automatic_supplementary_numbering(video, detection)
    recap_position = manual_recap_episode_number(video)
    if recap_position is not None:
        supplementary = SupplementaryNumberingHint("recap", recap_position)
    use_supplementary_identity = bool(
        supplementary is not None
        and supplementary.number is not None
        and (
            recap_position is not None
            or
            detection.is_supplementary
            or video.episode_number_manual_override is None
        )
    )
    if use_supplementary_identity:
        assert supplementary is not None and supplementary.number is not None
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
            "supplementary", supplementary.number,
            supplementary.supplementary_type, context_key, context_label,
            context_season_number,
        )
    if (
        video.season_episode_number is not None
        and not video.content_type_manual
        and (
            supplementary is None
            or video.episode_number_manual_override is not None
        )
    ):
        return VideoNumberingIdentity("standard", video.season_episode_number)
    return None


def _catalog_title_identity_key(
    video: Video,
    catalog_title: CatalogTitle | None = None,
) -> tuple[str, int] | None:
    title = catalog_title or video.catalog_title
    title_id = title.id if title is not None else video.catalog_title_id
    if title_id is not None:
        return ("id", title_id)
    if title is not None:
        return ("object", id(title))
    return None


def logical_episode_identity(
    video: Video,
    *,
    catalog_title: CatalogTitle | None = None,
    title_names: dict[str, list[CatalogTitle]] | None = None,
) -> LogicalEpisodeIdentity | None:
    """Derive the single shared standard-episode identity, never a variant key."""
    numbering = video_numbering_identity(video, title_names=title_names)
    title_key = _catalog_title_identity_key(video, catalog_title)
    if numbering is None or numbering.kind != "standard" or title_key is None:
        return None
    return LogicalEpisodeIdentity(title_key, numbering.number)


def _video_variant_group_id(video: Video) -> int | None:
    if video.video_variant_group_id is not None:
        return video.video_variant_group_id
    # Catalog/UI callers may intentionally evaluate detached rows. Reading the
    # instance dictionary preserves an explicitly attached transient group
    # without triggering a lazy load for the overwhelmingly common NULL case.
    group = video.__dict__.get("video_variant_group")
    return group.id if group is not None else None


def logical_episode_partitions(
    videos: list[Video],
    *,
    catalog_title: CatalogTitle | None = None,
) -> tuple[LogicalEpisodePartition, ...]:
    """Partition active standard videos by logical episode and confirmed lane.

    Confirmed duplicate secondaries and missing-primary remnants are not active
    representations. NULL stays an explicit unassigned bucket and never becomes
    a default variant.
    """
    title_names = supplementary_context_map(videos)
    by_identity: dict[LogicalEpisodeIdentity, list[Video]] = {}
    for video in videos:
        identity = logical_episode_identity(
            video,
            catalog_title=catalog_title,
            title_names=title_names,
        )
        if identity is not None and not is_nonprimary_duplicate_video(video):
            by_identity.setdefault(identity, []).append(video)

    partitions = []
    for identity, items in sorted(
        by_identity.items(),
        key=lambda item: (
            item[0].catalog_title_key,
            item[0].season_episode_number,
        ),
    ):
        ordered = tuple(sorted(items, key=deterministic_video_order_key))
        by_group: dict[int, list[Video]] = {}
        unassigned = []
        for video in ordered:
            group_id = _video_variant_group_id(video)
            if group_id is None:
                unassigned.append(video)
            else:
                by_group.setdefault(group_id, []).append(video)
        confirmed_variants = tuple(
            ConfirmedVideoVariantPartition(group_id, tuple(group_videos))
            for group_id, group_videos in sorted(by_group.items())
        )
        partitions.append(LogicalEpisodePartition(
            identity=identity,
            videos=ordered,
            confirmed_variants=confirmed_variants,
            unassigned_videos=tuple(unassigned),
        ))
    return tuple(partitions)


def unresolved_duplicate_groups(
    videos: list[Video],
    *,
    catalog_title: CatalogTitle | None = None,
) -> tuple[EpisodeDuplicateGroup, ...]:
    groups: list[EpisodeDuplicateGroup] = []
    for partition in logical_episode_partitions(videos, catalog_title=catalog_title):
        for items in partition.unresolved_video_groups:
            group_ids = {
                group_id
                for video in items
                if (group_id := _video_variant_group_id(video)) is not None
            }
            group_id = next(iter(group_ids)) if len(group_ids) == 1 else None
            group_video = next(
                (
                    video for video in items
                    if _video_variant_group_id(video) == group_id
                    and video.__dict__.get("video_variant_group") is not None
                ),
                None,
            ) if group_id is not None else None
            group_label = (
                group_video.__dict__["video_variant_group"].manual_label
                if group_video is not None
                else f"#{group_id}" if group_id is not None else None
            )
            groups.append(EpisodeDuplicateGroup(
                partition.identity.season_episode_number,
                items,
                video_variant_group_id=group_id,
                video_variant_label=group_label,
                has_unassigned_variant=bool(partition.unassigned_videos),
            ))

    # Supplementary identity and collision semantics intentionally stay exactly
    # as before Commit 2; variant lanes apply only to canonical standard episodes.
    by_identity: dict[VideoNumberingIdentity, list[Video]] = {}
    title_names = supplementary_context_map(videos)
    for video in videos:
        identity = video_numbering_identity(video, title_names=title_names)
        if (
            identity is not None
            and identity.kind == "supplementary"
            and not is_confirmed_duplicate(video)
        ):
            by_identity.setdefault(identity, []).append(video)
    groups.extend(
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
    return tuple(sorted(
        groups,
        key=lambda group: (
            group.supplementary_type or "",
            group.context_label or "",
            group.episode_number,
            group.video_variant_group_id or 0,
        ),
    ))


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


def confirmed_duplicate_variant_conflicts(
    videos: list[Video],
) -> tuple[EpisodeDuplicateGroup, ...]:
    """Return explicit duplicate relationships spanning distinct known lanes."""
    return tuple(
        group for group in confirmed_duplicate_groups(videos)
        if len({
            group_id
            for video in group.videos
            if (group_id := _video_variant_group_id(video)) is not None
        }) > 1
    )


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
    supplementary_hints = [
        automatic_supplementary_numbering(video, detection)
        for video, detection in zip(videos, detections)
    ]
    part_type = _numbering_part_type(title)
    title_is_supplemental = part_type in SUPPLEMENTAL_PART_TYPES
    local_values = [
        item.number
        if item.is_standard and hint is None and not title_is_supplemental
        else None
        for item, hint in zip(detections, supplementary_hints)
    ]
    automatic_values = [
        None if video.content_type_manual else local
        for video, local in zip(videos, local_values)
    ]
    effective_values = [
        (
            video.episode_number_manual_override
            if video.episode_number_manual_override is not None
            else local
        )
        if effective_video_content_type(video) != "recap"
        else None
        for video, local in zip(videos, automatic_values)
    ]
    numeric_values = [value for value in effective_values if value is not None]
    explicit_offset = title.episode_start_offset
    inferred_offset = (
        known_preceding_episodes
        if explicit_offset is None
        and known_preceding_episodes is not None
        else None
    )
    offset = explicit_offset if explicit_offset is not None else inferred_offset
    local_is_absolute = bool(offset is not None and numeric_values and min(numeric_values) > offset)
    has_external = title.metadata_record is not None if external_linked is None else external_linked

    for video, detection, supplementary_hint, local, effective in zip(
        videos, detections, supplementary_hints, local_values, effective_values
    ):
        video.local_episode_number = local
        if effective is None:
            video.season_episode_number = None
            video.absolute_episode_number = None
            video.external_episode_number = None
            if manual_recap_episode_number(video) is not None:
                source = "manual_recap"
            elif detection.is_supplementary:
                source = f"supplementary_{detection.supplementary_type}"
            elif supplementary_hint is not None and not title_is_supplemental:
                source = f"supplementary_{supplementary_hint.supplementary_type}"
            else:
                source = {
                    "zero": "nonstandard_zero",
                    "fractional": "fractional",
                    "structural_variant": "structural_variant",
                }.get(detection.kind, "unknown")
            video.episode_number_source = source
            video.episode_number_confidence = (
                0.95
                if (
                    detection.is_nonstandard
                    or detection.is_supplementary
                    or (
                        supplementary_hint is not None
                        and not title_is_supplemental
                    )
                )
                else None
            )
            continue
        is_manual = video.episode_number_manual_override is not None
        if title.numbering_mode == "absolute":
            absolute = effective
            season = effective - offset if offset is not None and effective > offset else None
        elif title.numbering_mode == "season_local":
            season = effective
            absolute = effective + offset if offset is not None else (
                effective if _can_start_absolute_sequence(title) else None
            )
        elif offset is not None:
            season = effective - offset if local_is_absolute else effective
            absolute = effective if local_is_absolute else effective + offset
        else:
            season = effective
            absolute = effective if _can_start_absolute_sequence(title) else None
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
    has_preceding_canonical_title = False
    for title in sorted(
        collection.titles,
        key=_numbering_title_sort_key,
    ):
        title_is_supplemental = (
            _numbering_part_type(title) in SUPPLEMENTAL_PART_TYPES
        )
        known = (
            preceding
            if (
                not title_is_supplemental
                and preceding_known
                and has_preceding_canonical_title
            )
            else None
        )
        recalculate_title_numbering(
            title, videos_by_title.get(title.id, []), known_preceding_episodes=known
        )
        if title_is_supplemental:
            continue
        has_preceding_canonical_title = True
        official_count = title.metadata_record.episode_count if title.metadata_record else None
        if official_count is not None:
            preceding += official_count
        else:
            preceding_known = False


def _can_start_absolute_sequence(title: CatalogTitle) -> bool:
    """Whether one isolated structural identity can safely begin at absolute E1."""
    season_number = _numbering_season_number(title)
    part_number = _numbering_part_number(title)
    part_type = _numbering_part_type(title)
    if season_number is not None:
        return season_number == 1 and (
            part_type not in {"part", "cour"} or part_number in {None, 1}
        )
    if part_type in {"part", "cour"} and part_number is not None:
        return part_number == 1
    return True


def _numbering_title_sort_key(title: CatalogTitle):
    """Keep Season and Part as separate axes with deterministic fallbacks."""
    return (
        _numbering_season_number(title) or 0,
        _numbering_part_number(title) or 0,
        _numbering_sort_order(title),
        title.relative_root_path.casefold(),
        title.id or 0,
    )


def _numbering_part_type(title: CatalogTitle) -> str:
    if (
        manual_hierarchy_snapshot_uses_legacy_projection(title)
        and title.part_type_manual is not None
    ):
        return title.part_type_manual
    return title.effective_part_type


def _numbering_season_number(title: CatalogTitle) -> int | None:
    if manual_hierarchy_snapshot_uses_legacy_projection(title):
        if title.part_type_manual is not None:
            return title.season_number_manual
        if title.season_number_manual is not None:
            return title.season_number_manual
    return title.effective_season_number


def _numbering_part_number(title: CatalogTitle) -> int | None:
    if manual_hierarchy_snapshot_uses_legacy_projection(title):
        return (
            title.part_number_manual
            if title.part_number_manual is not None
            else title.part_number
        )
    return title.effective_part_number


def _numbering_sort_order(title: CatalogTitle) -> int:
    if manual_hierarchy_snapshot_uses_legacy_projection(title):
        if title.sort_order_manual is not None:
            return title.sort_order_manual
        if title.season_number_manual is not None:
            return title.season_number_manual
    return title.effective_sort_order


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
    supplementary_type: str | None
    supplementary_number: int | Decimal | None

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
    video: Video,
    title: CatalogTitle | None = None,
    *,
    use_current_title: bool = True,
) -> EffectiveVideoNumbering:
    """Sjednotí manual/content/title autoritu nad automatickým filename parserem."""
    detection = detect_episode_number(video.filename)
    effective_title = (
        title
        if title is not None or not use_current_title
        else video.catalog_title
    )
    title_is_supplemental = bool(
        effective_title is not None
        and effective_title.effective_part_type in SUPPLEMENTAL_PART_TYPES
    )
    supplementary_hint = automatic_supplementary_numbering(video, detection)
    recap_position = manual_recap_episode_number(video)
    if recap_position is not None:
        supplementary_hint = SupplementaryNumberingHint("recap", recap_position)
    if video.content_type_manual or title_is_supplemental:
        classification = "supplementary"
    elif video.episode_number_manual_override is not None:
        classification = "standard"
    elif detection.is_supplementary:
        classification = "supplementary"
    elif detection.is_nonstandard:
        classification = "nonstandard"
    elif supplementary_hint is not None:
        classification = "supplementary"
    elif video.season_episode_number is not None or detection.is_standard:
        classification = "standard"
    else:
        classification = "unknown"
    numbering_input = (
        (
            video.episode_number_manual_override
            if video.episode_number_manual_override is not None
            else video.local_episode_number
            if video.local_episode_number is not None
            else detection.number if detection.is_standard else None
        )
        if classification == "standard"
        else None
    )
    return EffectiveVideoNumbering(
        detection=detection,
        classification=classification,
        season_episode_number=video.season_episode_number,
        numbering_input=numbering_input,
        manual_override=video.episode_number_manual_override is not None,
        supplementary_type=(
            supplementary_hint.supplementary_type
            if classification == "supplementary" and supplementary_hint is not None
            else None
        ),
        supplementary_number=(
            supplementary_hint.number
            if classification == "supplementary" and supplementary_hint is not None
            else None
        ),
    )


def summarize_title_numbering(
    videos: list[Video], title: CatalogTitle | None = None,
) -> TitleNumberingSummary:
    supplemental = bool(
        title is not None and title.effective_part_type in SUPPLEMENTAL_PART_TYPES
    )
    states = [effective_video_numbering(video, title) for video in videos]
    confirmed_duplicate = [is_nonprimary_duplicate_video(video) for video in videos]
    unnumbered_standard = 0 if supplemental else sum(
        state.is_standard
        and state.season_episode_number is None
        and not is_duplicate
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
    partitions = () if supplemental else logical_episode_partitions(
        videos,
        catalog_title=title,
    )
    unique_values = {
        partition.identity.season_episode_number for partition in partitions
    }
    episode_min = min(unique_values) if unique_values else None
    episode_max = max(unique_values) if unique_values else None
    gaps = tuple(
        sorted(set(range(episode_min, episode_max + 1)) - unique_values)
    ) if episode_min is not None and episode_max is not None else ()
    duplicate_groups = () if supplemental else unresolved_duplicate_groups(
        videos,
        catalog_title=title,
    )
    duplicates = tuple(sorted({
        group.episode_number
        for group in duplicate_groups
        if group.supplementary_type is None
    }))
    logical_episode_count = len(partitions)
    return TitleNumberingSummary(
        total=len(videos),
        standard_total=logical_episode_count,
        numbered=logical_episode_count,
        unnumbered_standard=unnumbered_standard,
        confirmed_variant_instance_count=sum(
            len(partition.confirmed_variants) for partition in partitions
        ),
        unassigned_variant_video_count=sum(
            len(partition.unassigned_videos) for partition in partitions
        ),
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
        variant_inconsistent_confirmed_duplicates=len(
            confirmed_duplicate_variant_conflicts(videos)
        ),
        supplemental=supplemental,
    )


def collection_requires_numbering_review(collection: CatalogCollection) -> bool:
    return any(
        summarize_title_numbering(list(title.videos), title).requires_review
        for title in collection.titles
    )


def effective_video_sort_position(
    video: Video,
    detection: EpisodeNumberDetection | None = None,
) -> Decimal | None:
    """Return the shared numeric presentation position, never a string key."""
    recap = manual_recap_episode_number(video)
    if recap is not None:
        return recap
    # Catalog presentation can sort ORM rows after their read-only session has
    # closed.  Reuse an eagerly loaded relationship when present, but never
    # trigger a lazy load merely to derive a numeric presentation key.
    loaded_title = video.__dict__.get("catalog_title")
    state = effective_video_numbering(
        video,
        title=loaded_title,
        use_current_title=False,
    )
    if state.is_standard and state.season_episode_number is not None:
        return Decimal(state.season_episode_number)
    detection = detection or state.detection
    return detection.sortable_episode_value


def _confirmed_expected_episode_count(title: CatalogTitle) -> int | None:
    """Use only manually confirmed primary metadata, never candidate guesses."""
    metadata = title.metadata_record
    if (
        metadata is None
        or metadata.episode_count is None
        or metadata.episode_count <= 0
        or title.metadata_status != "linked_manual"
    ):
        return None
    confirmed_primary = any(
        link.is_primary and link.is_manual and link.verified_at is not None
        for link in title.external_links
    )
    return metadata.episode_count if confirmed_primary else None


def _issue_code(issue: object) -> str:
    code = getattr(issue, "code", "")
    return str(getattr(code, "value", code))


def _issues_for_title_from_evaluation(
    title: CatalogTitle,
    issues: tuple[object, ...],
) -> tuple[object, ...]:
    title_video_ids = {video.id for video in title.videos if video.id is not None}
    return tuple(
        issue for issue in issues
        if getattr(issue, "catalog_title", None) is title
        or getattr(getattr(issue, "catalog_title", None), "id", None) == title.id
        or any(
            getattr(video, "id", None) in title_video_ids
            for video in getattr(issue, "videos", ())
        )
        or any(
            getattr(related, "id", None) == title.id
            for related in getattr(issue, "related_catalog_titles", ())
        )
    )


def _default_title_issues(title: CatalogTitle) -> tuple[object, ...]:
    collection = title.collection
    if collection is None:
        return ()
    # Local import avoids numbering <-> hierarchy_evaluation import recursion.
    from .hierarchy_evaluation import evaluate_collection_hierarchy

    evaluation = evaluate_collection_hierarchy(
        collection,
        list(collection.videos),
        include_legacy_fallback=False,
    )
    return _issues_for_title_from_evaluation(title, evaluation.issues)


def _bulk_renumber_fingerprint(
    title: CatalogTitle,
    *,
    gap_start: int,
    gap_end: int,
    offset: int,
    expected_count: int | None,
    issue_codes: tuple[str, ...],
) -> str:
    rows = [
        (
            video.id,
            video.catalog_collection_id,
            video.catalog_title_id,
            video.relative_path,
            video.file_type,
            video.content_type_manual,
            video.local_episode_number,
            video.season_episode_number,
            video.absolute_episode_number,
            video.external_episode_number,
            video.episode_number_manual_override,
            video.recap_episode_number_manual_tenths,
            video.video_variant_group_id,
            video.duplicate_of_video_id,
            bool(video.duplicate_primary_missing),
        )
        for video in sorted(title.videos, key=lambda item: (item.id or 0, item.relative_path))
    ]
    payload = (
        title.id,
        title.catalog_collection_id,
        title.effective_part_type,
        title.effective_season_number,
        title.effective_part_number,
        title.numbering_mode,
        title.episode_start_offset,
        title.hierarchy_manual_override,
        title.part_type_manual,
        title.season_number_manual,
        title.part_number_manual,
        title.metadata_status,
        expected_count,
        gap_start,
        gap_end,
        offset,
        issue_codes,
        rows,
    )
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def deterministic_bulk_renumber_proposal(
    title: CatalogTitle,
    *,
    issues: tuple[object, ...] | None = None,
    metrics: BulkRenumberMetrics | None = None,
) -> DeterministicBulkRenumberProposal | None:
    """Detect one unambiguous Recap-induced shift over logical episodes.

    The pass is title-scoped and linear after the shared logical partition index
    has been built.  It never mutates numbering or other authority.
    """
    if (
        title.id is None
        or title.effective_part_type in SUPPLEMENTAL_PART_TYPES
    ):
        return None
    title_videos_list = list(title.videos)
    metrics = metrics or BulkRenumberMetrics()
    metrics.physical_videos_scanned += len(title_videos_list)

    title_issues = _default_title_issues(title) if issues is None else tuple(issues)
    blocking_codes = {
        _issue_code(issue)
        for issue in title_issues
        if bool(getattr(issue, "blocking", False))
    }
    if blocking_codes - {"numbering_gap"}:
        return None

    summary = summarize_title_numbering(title_videos_list, title)
    if (
        summary.unnumbered_standard
        or summary.unknown
        or summary.nonstandard
        or summary.duplicate_numbers
        or summary.invalid_duplicate_references
        or summary.variant_inconsistent_confirmed_duplicates
        or unresolved_duplicate_groups(title_videos_list, catalog_title=title)
    ):
        return None

    partitions = logical_episode_partitions(
        title_videos_list,
        catalog_title=title,
    )
    metrics.logical_episodes_scanned += len(partitions)
    numbers = [partition.identity.season_episode_number for partition in partitions]
    if not numbers or numbers[0] != 1 or len(numbers) != len(set(numbers)):
        return None
    missing = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
    if not missing or missing != list(range(missing[0], missing[-1] + 1)):
        return None
    gap_start, gap_end = missing[0], missing[-1]
    suffix = [
        partition for partition in partitions
        if partition.identity.season_episode_number > gap_end
    ]
    if not suffix:
        return None
    suffix_numbers = [item.identity.season_episode_number for item in suffix]
    if suffix_numbers != list(range(suffix_numbers[0], suffix_numbers[-1] + 1)):
        return None
    offset = gap_start - suffix_numbers[0]
    if offset >= 0:
        return None

    gap_size = gap_end - gap_start + 1
    anchor_tenths = sorted({
        video.recap_episode_number_manual_tenths
        for video in title_videos_list
        if video.recap_episode_number_manual_tenths is not None
        and effective_video_content_type(video) == "recap"
        and video.recap_episode_number_manual_tenths % 10
        and video.recap_episode_number_manual_tenths // 10 == gap_start - 1
    })
    # One inserted fractional Recap explains one shifted slot; multiple exact
    # positions can safely explain a larger constant offset without hardcoding -1.
    if len(anchor_tenths) != gap_size:
        return None

    prefix_numbers = [number for number in numbers if number < gap_start]
    proposed_numbers = prefix_numbers + [number + offset for number in suffix_numbers]
    if proposed_numbers != list(range(1, proposed_numbers[-1] + 1)):
        return None

    expected_count = _confirmed_expected_episode_count(title)
    if expected_count is not None and (
        len(proposed_numbers) != expected_count
        or proposed_numbers[-1] != expected_count
    ):
        return None

    by_primary_id: dict[int, list[Video]] = {}
    for video in title_videos_list:
        if video.duplicate_of_video_id is not None:
            by_primary_id.setdefault(video.duplicate_of_video_id, []).append(video)

    rows: list[BulkRenumberLogicalChange] = []
    for partition in suffix:
        current = partition.identity.season_episode_number
        proposed = current + offset
        members: dict[int, Video] = {}
        for primary in partition.videos:
            if primary.id is None:
                return None
            members[primary.id] = primary
            for duplicate in by_primary_id.get(primary.id, ()):
                if duplicate.id is None:
                    return None
                members[duplicate.id] = duplicate
        physical_changes = []
        for video in sorted(members.values(), key=deterministic_video_order_key):
            if (
                not (
                    video.catalog_title is title
                    or video.catalog_title_id == title.id
                )
                or video.season_episode_number != current
                or effective_video_content_type(video) in SUPPLEMENTAL_PART_TYPES
                or video.recap_episode_number_manual_tenths is not None
            ):
                return None
            physical_changes.append(BulkRenumberPhysicalChange(
                video_id=video.id,
                filename=video.filename,
                current_episode=current,
                proposed_episode=proposed,
                manual_override=video.episode_number_manual_override is not None,
                confirmed_duplicate_secondary=(
                    video.duplicate_of_video_id is not None
                ),
                video_variant_group_id=video.video_variant_group_id,
            ))
        if not physical_changes:
            return None
        rows.append(BulkRenumberLogicalChange(
            current_episode=current,
            proposed_episode=proposed,
            physical_changes=tuple(physical_changes),
        ))

    if len({row.proposed_episode for row in rows}) != len(rows):
        return None
    if set(prefix_numbers) & {row.proposed_episode for row in rows}:
        return None

    warnings = tuple(
        message for message in (
            (
                "Návrh přepíše existující ruční čísla pouze po samostatném potvrzení."
                if any(row.has_manual_override for row in rows) else None
            ),
            (
                "Autoritativní expected episode count není dostupný; jednoznačnost "
                "vychází z lokální souvislé řady a explicitní fractional Recap pozice."
                if expected_count is None else None
            ),
        )
        if message is not None
    )
    issue_codes = tuple(sorted({_issue_code(issue) for issue in title_issues}))
    return DeterministicBulkRenumberProposal(
        catalog_title_id=title.id,
        title_name=title.local_title,
        gap_start=gap_start,
        gap_end=gap_end,
        offset=offset,
        recap_positions=tuple(
            format_episode_position(Decimal(tenths) / Decimal(10))
            for tenths in anchor_tenths
        ),
        rows=tuple(rows),
        expected_episode_count=expected_count,
        expected_count_authoritative=expected_count is not None,
        warnings=warnings,
        fingerprint=_bulk_renumber_fingerprint(
            title,
            gap_start=gap_start,
            gap_end=gap_end,
            offset=offset,
            expected_count=expected_count,
            issue_codes=issue_codes,
        ),
    )


def apply_deterministic_bulk_renumber(
    session: Session,
    catalog_title_id: int,
    *,
    expected_fingerprint: str,
    confirm_manual_overrides: bool = False,
) -> DeterministicBulkRenumberProposal:
    """Revalidate and atomically apply one freshly confirmed proposal."""
    title = session.get(CatalogTitle, catalog_title_id)
    if title is None or title.collection is None:
        raise ValueError("Část pro hromadné přečíslování nebyla nalezena.")

    # One shared evaluation is reused by the resolver; no per-row/library scan.
    from .hierarchy_evaluation import (
        evaluate_collection_hierarchy,
        finalize_hierarchy_write,
    )

    evaluation = evaluate_collection_hierarchy(
        title.collection,
        list(title.collection.videos),
        include_legacy_fallback=False,
    )
    title_issues = _issues_for_title_from_evaluation(title, evaluation.issues)
    proposal = deterministic_bulk_renumber_proposal(title, issues=title_issues)
    if (
        proposal is None
        or not expected_fingerprint
        or proposal.fingerprint != expected_fingerprint
    ):
        raise ValueError(
            "Náhled přečíslování je zastaralý nebo již není jednoznačný; "
            "načtěte nový návrh."
        )
    if proposal.has_manual_overrides and not confirm_manual_overrides:
        raise ValueError(
            "Přepsání existujících ručních čísel je nutné samostatně potvrdit."
        )

    videos_by_id = {video.id: video for video in title.videos}
    expected_final_numbers = tuple(
        range(1, proposal.rows[-1].proposed_episode + 1)
    )
    with session.begin_nested():
        for row in proposal.rows:
            for change in row.physical_changes:
                video = videos_by_id.get(change.video_id)
                if video is None:
                    raise ValueError("Membership návrhu se během potvrzení změnil.")
                set_video_episode_override(video, change.proposed_episode)
        session.flush()
        finalize_hierarchy_write([title.collection])
        session.flush()

        final_partitions = logical_episode_partitions(
            list(title.videos), catalog_title=title
        )
        final_numbers = tuple(
            partition.identity.season_episode_number
            for partition in final_partitions
        )
        if final_numbers != expected_final_numbers:
            raise ValueError(
                "Výsledná canonical řada neodpovídá potvrzenému náhledu; "
                "operace byla vrácena zpět."
            )
        if unresolved_duplicate_groups(list(title.videos), catalog_title=title):
            raise ValueError(
                "Přečíslování by vytvořilo canonical collision; operace byla vrácena zpět."
            )
    return proposal
