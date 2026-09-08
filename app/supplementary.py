"""Read-only local supplementary identity, independent of provider numbering."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import re

from .catalog import (
    EpisodeNumberDetection, detect_episode_number,
    effective_video_content_type,
)
from .hierarchy_types import VIDEO_CONTENT_TYPES, VIDEO_CONTENT_TYPE_LABELS
from .models import CatalogTitle, Video


# PV and Preview already share one parser subtype; do not invent a second axis.
ORDINAL_TYPES = VIDEO_CONTENT_TYPES - {"episode"}
ORDINAL_LABELS = VIDEO_CONTENT_TYPE_LABELS


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
    manual = (video.content_type_manual or "").strip()
    title_type = title.effective_part_type if title is not None else None
    subtype = effective_video_content_type(
        video, title, detection=detection, use_current_title=False,
    )
    if subtype not in ORDINAL_TYPES:
        return None
    if subtype == "recap":
        from .numbering import effective_recap_episode_number
        if effective_recap_episode_number(
            video, title, detection=detection, use_current_title=False,
        ) is not None:
            return None
    # Raw Other is also the parser's unresolved fallback. Unknown content and
    # zero/fractional/A-B evidence in a main container keep their diagnostics;
    # explicit Other (video or container) is still a normal typed identity.
    if (subtype == "other" and not manual
            and title_type not in ORDINAL_TYPES):
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
    from .media_parts import media_part_total
    # Variant groups belong to one title; unlike explicit physical segments,
    # groups from different titles cannot explain a shared ordinal.
    if any(variant_group_id(v) is not None for v in videos) and len({
        v.catalog_title_id if v.catalog_title_id is not None
        else id(v.__dict__.get("catalog_title")) for v in videos
    }) > 1:
        return True
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
        elif None in parts or media_part_total(items) != len(items):
            return True
    return False


@dataclass(frozen=True)
class SupplementaryIdentity:
    catalog_title_key: tuple
    supplementary_type: str
    ordinal: int | None


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
    unnumbered_identity_groups: tuple[tuple[Video, ...], ...] = ()

    @property
    def logical_identity_count(self) -> int | None:
        """Multiplicity including unidentified items, never physical copies."""
        if self.invalid_duplicates or any(p.requires_review for p in self.partitions):
            return None
        return len(self.partitions) + len(self.unnumbered_identity_groups)

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


@dataclass(frozen=True)
class TypedStructuralContext:
    key: tuple
    label: str


def typed_structural_contexts(
    videos: list[Video], titles: list[CatalogTitle] | None = None,
) -> dict[Video, TypedStructuralContext]:
    """Reuse collection attachment authority, without lazy loads or inference.

    Callers provide all collection titles, including primaries without videos.
    An already loaded collection can supply them for standalone inventory calls.
    """
    from .collection_presentation import build_collection_presentation

    known = {id(t): t for t in titles or []}
    seen_collections = set()
    for video in videos:
        title = video.__dict__.get("catalog_title")
        if title is not None:
            known[id(title)] = title
            collection = title.__dict__.get("collection")
            if collection is not None and id(collection) not in seen_collections:
                seen_collections.add(id(collection))
                for sibling in collection.__dict__.get("titles", ()):
                    known[id(sibling)] = sibling

    def collection_key(title, video=None):
        identifier = title.catalog_collection_id if title is not None else None
        if identifier is None and video is not None:
            identifier = video.catalog_collection_id
        if identifier is not None:
            return ("collection", identifier)
        collection = title.__dict__.get("collection") if title is not None else None
        return ("collection_object", id(collection)) if collection is not None else (
            "unattached_title", title.id if title is not None and title.id is not None else id(title),
        )

    grouped = defaultdict(list)
    for title in known.values():
        grouped[collection_key(title)].append(title)
    contexts = {}
    for boundary, members in grouped.items():
        presentation = build_collection_presentation(members, include_videos=False)
        for part in presentation.primary_parts:
            primary = part.title
            key = ("primary", primary.id) if primary.id is not None else ("primary_object", id(primary))
            context = TypedStructuralContext(
                (boundary, key), primary.effective_season_label or primary.local_title,
            )
            contexts[id(primary)] = context
            for child in part.supplementary_parts:
                contexts[id(child.title)] = context
        for part in presentation.anime_level_parts:
            contexts[id(part.title)] = TypedStructuralContext((boundary, ("root",)), "Anime / root")
    by_id = {t.id: t for t in known.values() if t.id is not None}
    result = {}
    for video in videos:
        title = video.__dict__.get("catalog_title") or by_id.get(video.catalog_title_id)
        result[video] = contexts.get(id(title), TypedStructuralContext(
            (collection_key(title, video), ("root",)), "Anime / root",
        ))
    return result


def supplementary_inventory(
    videos: list[Video], title: CatalogTitle | None = None,
    *, collection_scope: bool = False, titles_by_id: dict[int, CatalogTitle] | None = None,
    contexts: dict[Video, TypedStructuralContext] | None = None,
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
    scopes = {}
    if collection_scope and contexts is None:
        contexts = typed_structural_contexts(videos, list((titles_by_id or {}).values()))
    for video in videos:
        current = title or (titles_by_id or {}).get(video.catalog_title_id) or video.__dict__.get("catalog_title")
        state = supplementary_ordinal(video, current)
        if state is None:
            continue
        title_id = current.id if current is not None else video.catalog_title_id
        key = (
            ("id", title_id) if title_id is not None
            else ("object", id(current)) if current is not None else None
        )
        scopes[video] = key
        if collection_scope:
            key = contexts[video].key
        identity = (
            SupplementaryIdentity(key, state.supplementary_type, state.number)
            if key is not None else None
        )
        identities[video] = identity
        if not (
            video.duplicate_of_video_id is not None
            or video.__dict__.get("duplicate_of") is not None
            or video.duplicate_primary_missing
        ):
            if state.number is None or identity is None:
                unknown.append(video)
            else:
                by_identity[identity].append(video)
    for video, identity in identities.items():
        primary = video.__dict__.get("duplicate_of") or by_id.get(video.duplicate_of_video_id)
        if video.duplicate_primary_missing or video.duplicate_of_video_id is not None or primary is not None:
            if (primary is None or primary not in identities or identity is None
                or identities[primary] != identity or primary is video
                or scopes[primary] != scopes[video]
                or primary.duplicate_of_video_id is not None
                or primary.__dict__.get("duplicate_of") is not None
                or primary.duplicate_primary_missing
                or (variant_group_id(video) is not None and variant_group_id(primary) is not None
                    and variant_group_id(video) != variant_group_id(primary))
                or video.media_part_number != primary.media_part_number):
                invalid.append(video)
    unnumbered = defaultdict(list)
    for video in unknown:
        # Physical-part authority is title-local; never combine two containers
        # merely because both have an unidentified item of the same type.
        unnumbered[(scopes[video], identities[video])].append(video)
    groups = []
    for items in unnumbered.values():
        if (all(v.media_part_number is not None for v in items)
                and not representation_conflict(items)):
            groups.append(tuple(items))
        else:
            groups.extend((video,) for video in items)
    return SupplementaryInventory(
        tuple(SupplementaryPartition(
            identity, tuple(items),
            representation_conflict(items),
        ) for identity, items in by_identity.items()),
        tuple(unknown), tuple(invalid), tuple(groups),
    )


def supplementary_review_issues(
    videos: list[Video], title: CatalogTitle | None = None, *,
    collection_scope: bool = False, titles_by_id: dict[int, CatalogTitle] | None = None,
) -> tuple[SupplementaryReviewIssue, ...]:
    """Return collision-risk reasons for Hierarchy Review using loaded scalar data.

    An unnumbered logical singleton needs no ordinal. Multiplicity uses the
    shared inventory's representation rules, never the physical row count.
    Collection callers pass the whole collection and project issues afterwards.
    """
    contexts = typed_structural_contexts(
        videos, list((titles_by_id or {}).values()),
    ) if collection_scope else None
    typed = defaultdict(list)
    for video in videos:
        current = title or (titles_by_id or {}).get(video.catalog_title_id)
        state = supplementary_ordinal(video, current)
        if state is not None:
            context = contexts[video] if contexts is not None else None
            typed[(context, state.supplementary_type)].append((video, state))

    issues = []
    for context, subtype in sorted(typed, key=lambda key: (repr(key[0]), key[1])):
        members = typed[(context, subtype)]
        issue_start = len(issues)
        inventory = supplementary_inventory(
            [video for video, _state in members], title, collection_scope=collection_scope,
            titles_by_id=titles_by_id, contexts=contexts,
        )
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
        count = inventory.logical_identity_count
        if (count is None or count > 1) and missing:
            issues.append(SupplementaryReviewIssue(
                code="missing_supplementary_ordinal",
                supplementary_type=subtype,
                videos=missing,
                known_ordinals=known_ordinals,
                message=(
                    "Více logických identit stejného typu v tomto kontextu nelze bezpečně rozlišit: "
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
        if context is not None:
            for index in range(issue_start, len(issues)):
                issues[index] = replace(issues[index], message=(
                    f"Kontext: {context.label} / {ORDINAL_LABELS[subtype]}. "
                    + issues[index].message
                ))
    return tuple(issues)


def collection_supplementary_review(
    videos: list[Video], titles: list[CatalogTitle] | None = None,
) -> dict[int, tuple[SupplementaryReviewIssue, ...]]:
    """Build collection-wide review once, then project issues to title cards."""
    by_title = defaultdict(list)
    for issue in supplementary_review_issues(
        videos, collection_scope=True, titles_by_id={t.id: t for t in titles or []},
    ):
        members = defaultdict(list)
        for video in issue.videos:
            title = video.__dict__.get("catalog_title")
            key = title.id if title is not None else video.catalog_title_id
            members[key].append(video)
        for key, items in members.items():
            by_title[key].append(replace(issue, videos=tuple(items)))
    return {key: tuple(issues) for key, issues in by_title.items()}


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
