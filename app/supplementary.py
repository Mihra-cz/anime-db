"""Read-only local supplementary identity, independent of provider numbering."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re

from .catalog import (
    EpisodeNumberDetection, detect_episode_number,
    normalize_supplementary_subtype,
)
from .models import CatalogTitle, Video


# PV and Preview already share one parser subtype; do not invent a second axis.
ORDINAL_TYPES = frozenset({"op", "ed", "ncop", "nced", "ova", "special", "preview", "cm"})
ORDINAL_LABELS = {
    "op": "OP", "ed": "ED", "ncop": "NCOP", "nced": "NCED", "ova": "OVA",
    "special": "Special", "preview": "Preview", "cm": "CM",
}


@dataclass(frozen=True)
class SupplementaryOrdinal:
    supplementary_type: str
    number: int | None
    source: str

    @property
    def type_label(self) -> str:
        return ORDINAL_LABELS[self.supplementary_type]

    @property
    def display_label(self) -> str:
        return (
            f"{self.type_label} {self.number:02d}"
            if self.number is not None else self.type_label
        )


def supplementary_ordinal(
    video: Video, title: CatalogTitle | None = None, *,
    detection: EpisodeNumberDetection | None = None,
    use_current_title: bool = True,
) -> SupplementaryOrdinal | None:
    """Manual authority > explicit filename evidence > unknown; never row order.

    Relationships must already be loaded (or title passed explicitly). Generic
    Episode 14, canonical/external numbers and physical parts are not ordinals.
    """
    detection = detection or detect_episode_number(video.filename)
    if title is None and use_current_title:
        title = video.__dict__.get("catalog_title")
    manual = normalize_supplementary_subtype((video.content_type_manual or "").strip())
    raw = normalize_supplementary_subtype((video.file_type or "").strip())
    title_type = title.effective_part_type if title is not None else None
    subtype = (
        manual if video.content_type_manual is not None
        else detection.supplementary_type if detection.supplementary_type in ORDINAL_TYPES
        else raw if raw in ORDINAL_TYPES
        else title_type
    )
    if subtype not in ORDINAL_TYPES:
        return None
    override = video.episode_number_manual_override
    if override is not None:
        # Preserve existing canonical override semantics for generic filename
        # numbers inside a Season; exact supplementary evidence stays local.
        if (
            not manual
            and title_type not in ORDINAL_TYPES | {"bonus", "other", "film"}
            and not detection.is_supplementary
        ):
            return None
        return SupplementaryOrdinal(subtype, override if override > 0 else None, "manual")
    number = None
    marker = {
        "ova": "ova|oad", "special": "specials?", "preview": "previews?|pv",
    }.get(subtype, subtype)
    if detection.supplementary_type == subtype:
        number = detection.supplementary_number
    elif (
        detection.is_standard
        and re.search(rf"(?<![a-z0-9])(?:{marker})(?![a-z0-9])", video.filename, re.I)
    ):
        # The marker must be in the filename, never just in its parent folder.
        number = detection.number
    if (
        video.media_part_number is not None
        and re.search(r"\b(?:OVA|OAD)\s+P\d+", video.filename, re.I)
    ):
        number = None
    number = number if number is not None and number > 0 else None
    return SupplementaryOrdinal(subtype, number, "parser" if number is not None else "unknown")


def variant_group_id(video: Video) -> int | None:
    if video.video_variant_group_id is not None:
        return video.video_variant_group_id
    group = video.__dict__.get("video_variant_group")
    return group.id if group else None


def representation_conflict(videos: list[Video]) -> bool:
    """Distinct confirmed lanes and complete physical parts are separate axes."""
    lanes: dict[int | None, list[Video]] = defaultdict(list)
    for video in videos:
        lanes[variant_group_id(video)].append(video)
    if len(lanes) > 1 and None in lanes:
        return True
    for items in lanes.values():
        parts = [video.media_part_number for video in items]
        if all(part is None for part in parts):
            if len(items) > 1:
                return True
        elif (len(parts) < 2 or None in parts or len(set(parts)) != len(parts)
              or min(parts) != 1 or max(parts) != len(parts)):
            return True
    return False


@dataclass(frozen=True)
class SupplementaryIdentity:
    catalog_title_key: tuple[str, int]
    supplementary_type: str
    ordinal: int


@dataclass(frozen=True)
class SupplementaryPartition:
    identity: SupplementaryIdentity
    videos: tuple[Video, ...]
    requires_review: bool


@dataclass(frozen=True)
class SupplementaryInventory:
    partitions: tuple[SupplementaryPartition, ...]
    unknown_videos: tuple[Video, ...]
    invalid_duplicates: tuple[Video, ...]

    @property
    def requires_review(self) -> bool:
        return bool(
            self.unknown_videos or self.invalid_duplicates
            or any(p.requires_review for p in self.partitions)
        )

    @property
    def logical_count(self) -> int | None:
        return None if self.requires_review else len(self.partitions)


@dataclass(frozen=True)
class SupplementaryReviewIssue:
    """Derived Hierarchy Review item; it never changes hierarchy authority."""

    code: str
    supplementary_type: str
    videos: tuple[Video, ...]
    known_ordinals: tuple[int, ...]
    message: str

    @property
    def type_label(self) -> str:
        return ORDINAL_LABELS[self.supplementary_type]

    @property
    def issue_label(self) -> str:
        return {
            "missing_supplementary_ordinal": "Chybějící supplementary ordinal",
            "supplementary_ordinal_collision": "Kolize supplementary ordinalu",
            "broken_supplementary_identity": "Neplatná nebo nejednoznačná supplementary identita",
        }[self.code]


def supplementary_inventory(
    videos: list[Video], title: CatalogTitle | None = None,
) -> SupplementaryInventory:
    """Linear, in-memory precondition for future naming; no target paths/writes.

    An ordinal is evidence even when its representations collide. Only confirmed
    duplicates, confirmed distinct variant lanes and complete Media Part sets
    resolve a collision. Unknowns never receive an inferred singleton ordinal.
    """
    by_identity: dict[SupplementaryIdentity, list[Video]] = defaultdict(list)
    unknown, invalid = [], []
    by_id = {v.id: v for v in videos if v.id is not None}
    identities = {}
    for video in videos:
        state = supplementary_ordinal(video, title)
        if state is None:
            continue
        current = title if title is not None else video.__dict__.get("catalog_title")
        title_id = current.id if current is not None else video.catalog_title_id
        key = (
            ("id", title_id) if title_id is not None
            else ("object", id(current)) if current is not None else None
        )
        identity = (
            SupplementaryIdentity(key, state.supplementary_type, state.number)
            if key is not None and state.number is not None else None
        )
        identities[video] = identity
        if identity is None:
            unknown.append(video)
        elif not (
            video.duplicate_of_video_id is not None
            or video.__dict__.get("duplicate_of") is not None
            or video.duplicate_primary_missing
        ):
            by_identity[identity].append(video)
    for video, identity in identities.items():
        primary = video.__dict__.get("duplicate_of") or by_id.get(video.duplicate_of_video_id)
        if video.duplicate_primary_missing or video.duplicate_of_video_id is not None or primary is not None:
            if (primary is None or primary not in identities or identity is None
                or identities[primary] != identity or primary is video
                or primary.duplicate_of_video_id is not None
                or primary.__dict__.get("duplicate_of") is not None
                or primary.duplicate_primary_missing
                or (variant_group_id(video) is not None and variant_group_id(primary) is not None
                    and variant_group_id(video) != variant_group_id(primary))
                or video.media_part_number != primary.media_part_number):
                invalid.append(video)
    return SupplementaryInventory(
        tuple(SupplementaryPartition(identity, tuple(items), representation_conflict(items))
              for identity, items in by_identity.items()), tuple(unknown), tuple(invalid),
    )


def supplementary_review_issues(
    videos: list[Video], title: CatalogTitle,
) -> tuple[SupplementaryReviewIssue, ...]:
    """Return collision-risk reasons for Hierarchy Review using loaded scalar data.

    A single unnumbered supplementary video cannot collide with another item in
    its local type namespace, so it remains unknown without opening this review
    workflow. Multiple items of the same type must all have safe identities.
    """
    typed: dict[str, list[tuple[Video, SupplementaryOrdinal]]] = defaultdict(list)
    for video in videos:
        state = supplementary_ordinal(video, title)
        if state is not None:
            typed[state.supplementary_type].append((video, state))

    issues = []
    for subtype in sorted(typed):
        members = typed[subtype]
        inventory = supplementary_inventory([video for video, _state in members], title)
        known_ordinals = tuple(sorted({
            state.number for _video, state in members if state.number is not None
        }))
        invalid_ids = {id(video) for video in inventory.invalid_duplicates}
        missing = tuple(sorted(
            (
                video for video in inventory.unknown_videos
                if id(video) not in invalid_ids
            ),
            key=lambda video: (video.relative_path.casefold(), video.id or 0),
        ))
        if len(members) > 1 and missing:
            issues.append(SupplementaryReviewIssue(
                code="missing_supplementary_ordinal",
                supplementary_type=subtype,
                videos=missing,
                known_ordinals=known_ordinals,
                message=(
                    "Více videí stejného supplementary typu nelze bezpečně rozlišit: "
                    "u uvedených souborů chybí explicitní ruční nebo parserový ordinal."
                ),
            ))
        for partition in sorted(
            (item for item in inventory.partitions if item.requires_review),
            key=lambda item: item.identity.ordinal,
        ):
            issues.append(SupplementaryReviewIssue(
                code="supplementary_ordinal_collision",
                supplementary_type=subtype,
                videos=tuple(sorted(
                    partition.videos,
                    key=lambda video: (video.relative_path.casefold(), video.id or 0),
                )),
                known_ordinals=(partition.identity.ordinal,),
                message=(
                    f"Ordinal {partition.identity.ordinal:02d} sdílí více fyzických videí "
                    "bez potvrzené authority, která by bezpečně vysvětlila varianty, "
                    "duplicity nebo Media Parts."
                ),
            ))
        if inventory.invalid_duplicates:
            issues.append(SupplementaryReviewIssue(
                code="broken_supplementary_identity",
                supplementary_type=subtype,
                videos=tuple(sorted(
                    inventory.invalid_duplicates,
                    key=lambda video: (video.relative_path.casefold(), video.id or 0),
                )),
                known_ordinals=known_ordinals,
                message=(
                    "Potvrzená duplicate vazba nevede na stejnou bezpečnou "
                    "supplementary identitu nebo její primární video chybí."
                ),
            ))
    return tuple(issues)


def supplementary_media_siblings(videos: list[Video]) -> dict[Video, list[Video]]:
    """Scope Media Part labels by known ordinal and confirmed lane in one pass."""
    groups: dict[tuple, list[Video]] = defaultdict(list)
    keys = {}
    for video in videos:
        state = supplementary_ordinal(video)
        if state is not None and state.number is not None:
            title = video.__dict__.get("catalog_title")
            key = (title.id if title is not None else video.catalog_title_id,
                   state.supplementary_type, state.number, variant_group_id(video))
            groups[key].append(video)
            keys[video] = key
    return {video: groups[key] for video, key in keys.items()}
