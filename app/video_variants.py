from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .catalog import detect_episode_number
from .models import CatalogCollection, CatalogTitle, Video, VideoVariantGroup, utc_now
from .numbering import (
    is_nonprimary_duplicate_video,
    logical_episode_identity,
    set_video_episode_override,
)


VIDEO_VARIANT_RELEASE_SOURCE_CHOICES: tuple[tuple[str, str], ...] = (
    ("tv", "TV"),
    ("bd", "BD"),
    ("web", "WEB"),
    ("dvd", "DVD"),
    ("other", "Other"),
)
VIDEO_VARIANT_RELEASE_SOURCES = frozenset(
    value for value, _label in VIDEO_VARIANT_RELEASE_SOURCE_CHOICES
)
VIDEO_VARIANT_CONTENT_VARIANT_CHOICES: tuple[tuple[str, str], ...] = (
    ("censored", "Censored"),
    ("uncensored", "Uncensored"),
    ("other", "Other"),
)
VIDEO_VARIANT_CONTENT_VARIANTS = frozenset(
    value for value, _label in VIDEO_VARIANT_CONTENT_VARIANT_CHOICES
)


_UNSPECIFIED_GROUP = object()
_UNSPECIFIED_TITLE = object()


CONFIRMED_DUPLICATE_VARIANT_CONFLICT_MESSAGE = (
    "Potvrzená duplicita nemůže být rozdělena do dvou různých potvrzených "
    "variant. Nejprve upravte duplicate vztah."
)


@dataclass(frozen=True)
class ParserVariantSuggestion:
    hint: str
    manual_label: str
    release_source: str | None = None
    content_variant: str | None = None


@dataclass(frozen=True)
class VariantGroupDraft:
    """One explicitly selected existing group or one unpersisted manual draft."""

    key: str
    existing_group_id: int | None = None
    manual_label: str = ""
    release_source: str | None = None
    content_variant: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class VariantAssignmentPreviewRow:
    video_id: int
    filename: str
    episode_label: str
    current_label: str
    target_label: str
    parser_hint: str | None


@dataclass(frozen=True)
class VariantAssignmentPreview:
    catalog_title_id: int
    workflow: str
    rows: tuple[VariantAssignmentPreviewRow, ...]
    drafts: tuple[VariantGroupDraft, ...]
    assignments: tuple[tuple[int, str], ...]
    groups_to_create: tuple[str, ...]
    unresolved_collisions_before: int
    unresolved_collisions_after: int
    duplicate_collisions_after: int
    new_blockers: tuple[str, ...]
    fingerprint: str

    @property
    def resolved_collision_count(self) -> int:
        return max(
            0,
            self.unresolved_collisions_before - self.unresolved_collisions_after,
        )


@dataclass(frozen=True)
class VariantLanePair:
    episode_number: int
    hinted_video_id: int
    hinted_filename: str
    plain_video_id: int
    plain_filename: str


@dataclass(frozen=True)
class VariantLaneProposal:
    catalog_title_id: int
    parser_hint: str
    pairs: tuple[VariantLanePair, ...]
    hinted_suggestion: ParserVariantSuggestion
    fingerprint: str


@dataclass(frozen=True)
class StructuralABPairProposal:
    catalog_title_id: int
    episode_number: int
    video_a_id: int
    filename_a: str
    video_b_id: int
    filename_b: str
    fingerprint: str


@dataclass(frozen=True)
class StructuralABPreview:
    proposal: StructuralABPairProposal
    assignment_preview: VariantAssignmentPreview

    @property
    def row_a(self) -> VariantAssignmentPreviewRow:
        return next(
            row for row in self.assignment_preview.rows
            if row.video_id == self.proposal.video_a_id
        )

    @property
    def row_b(self) -> VariantAssignmentPreviewRow:
        return next(
            row for row in self.assignment_preview.rows
            if row.video_id == self.proposal.video_b_id
        )


def _normalize_manual_label(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Označení video variant group nesmí být prázdné.")
    return normalized


def _normalize_taxonomy_value(
    value: str | None,
    supported: frozenset[str],
    *,
    field_label: str,
) -> str | None:
    normalized = value.strip().casefold() if value is not None else ""
    if not normalized:
        return None
    if normalized not in supported:
        raise ValueError(f"Neplatná hodnota {field_label}.")
    return normalized


def _titles_match(first: CatalogTitle | None, second: CatalogTitle | None) -> bool:
    if first is None or second is None:
        return first is second
    if first is second:
        return True
    return first.id is not None and first.id == second.id


def _require_persisted_title(title: CatalogTitle | None) -> CatalogTitle:
    if title is None or title.id is None:
        raise ValueError("Video variant group musí patřit existujícímu CatalogTitle.")
    return title


def validate_video_variant_group(group: VideoVariantGroup) -> None:
    """Validate the persisted manual authority fields without changing the group."""
    if group.id is None:
        raise ValueError("Video variant group musí být před přiřazením uložená.")
    _require_persisted_title(group.catalog_title)
    _normalize_manual_label(group.manual_label)
    _normalize_taxonomy_value(
        group.release_source,
        VIDEO_VARIANT_RELEASE_SOURCES,
        field_label="release source",
    )
    _normalize_taxonomy_value(
        group.content_variant,
        VIDEO_VARIANT_CONTENT_VARIANTS,
        field_label="content variant",
    )


def create_video_variant_group(
    catalog_title: CatalogTitle,
    *,
    manual_label: str,
    release_source: str | None = None,
    content_variant: str | None = None,
    note: str | None = None,
    verified_at: datetime | None = None,
) -> VideoVariantGroup:
    """Build one explicit manual-authority lane for an already stored title."""
    _require_persisted_title(catalog_title)
    return VideoVariantGroup(
        catalog_title=catalog_title,
        manual_label=_normalize_manual_label(manual_label),
        release_source=_normalize_taxonomy_value(
            release_source,
            VIDEO_VARIANT_RELEASE_SOURCES,
            field_label="release source",
        ),
        content_variant=_normalize_taxonomy_value(
            content_variant,
            VIDEO_VARIANT_CONTENT_VARIANTS,
            field_label="content variant",
        ),
        note=(note or "").strip() or None,
        verified_at=verified_at or utc_now(),
    )


def update_video_variant_group(
    group: VideoVariantGroup,
    *,
    manual_label: str,
    release_source: str | None = None,
    content_variant: str | None = None,
    note: str | None = None,
    verified_at: datetime | None = None,
) -> None:
    """Update mutable classification fields while preserving the stable group ID."""
    validate_video_variant_group(group)
    normalized_label = _normalize_manual_label(manual_label)
    normalized_source = _normalize_taxonomy_value(
        release_source,
        VIDEO_VARIANT_RELEASE_SOURCES,
        field_label="release source",
    )
    normalized_content = _normalize_taxonomy_value(
        content_variant,
        VIDEO_VARIANT_CONTENT_VARIANTS,
        field_label="content variant",
    )
    group.manual_label = normalized_label
    group.release_source = normalized_source
    group.content_variant = normalized_content
    group.note = (note or "").strip() or None
    group.verified_at = verified_at or utc_now()


def validate_video_variant_assignment(
    video: Video,
    group: VideoVariantGroup,
    *,
    catalog_title: CatalogTitle | None | object = _UNSPECIFIED_TITLE,
) -> None:
    """Reject a cross-title assignment; never repairs or guesses a target group."""
    validate_video_variant_group(group)
    title = (
        video.catalog_title
        if catalog_title is _UNSPECIFIED_TITLE
        else cast(CatalogTitle | None, catalog_title)
    )
    if title is None or not _titles_match(title, group.catalog_title):
        raise ValueError(
            "Video lze přiřadit pouze k variant group ze stejného CatalogTitle."
        )


def _known_group_identity(video: Video) -> tuple[str, int] | None:
    group_id = video.video_variant_group_id
    if group_id is None:
        group = video.__dict__.get("video_variant_group")
        group_id = group.id if group is not None else None
    return ("group", group_id) if group_id is not None else None


def _validate_direct_duplicate_assignment(
    video: Video,
    group: VideoVariantGroup | None,
) -> None:
    target = ("group", group.id) if group is not None and group.id is not None else None
    related = [*video.duplicate_copies]
    if video.duplicate_of is not None:
        related.append(video.duplicate_of)
    for other in related:
        other_group = _known_group_identity(other)
        if target is not None and other_group is not None and target != other_group:
            raise ValueError(CONFIRMED_DUPLICATE_VARIANT_CONFLICT_MESSAGE)


def assign_video_variant_group(
    video: Video,
    group: VideoVariantGroup | None,
) -> None:
    """Apply or clear a manually confirmed group assignment."""
    if group is not None:
        validate_video_variant_assignment(video, group)
    _validate_direct_duplicate_assignment(video, group)
    video.video_variant_group = group


def assign_video_catalog_title(
    video: Video,
    catalog_title: CatalogTitle | None,
    *,
    video_variant_group: VideoVariantGroup | None | object = _UNSPECIFIED_GROUP,
) -> None:
    """Set title membership and atomically keep, replace, or clear variant authority.

    Without an explicit new group, an existing assignment survives only when it
    belongs to the target title. No corresponding group is inferred or cloned.
    """
    if video_variant_group is _UNSPECIFIED_GROUP:
        current_group = video.video_variant_group
        target_group = (
            current_group
            if current_group is not None
            and _titles_match(catalog_title, current_group.catalog_title)
            else None
        )
    else:
        target_group = cast(VideoVariantGroup | None, video_variant_group)
        if target_group is not None:
            validate_video_variant_assignment(
                video,
                target_group,
                catalog_title=catalog_title,
            )
            _validate_direct_duplicate_assignment(video, target_group)

    video.catalog_title = catalog_title
    video.video_variant_group = target_group


def parser_variant_suggestion(video: Video) -> ParserVariantSuggestion | None:
    """Translate parser evidence into an editable, never-persisted UI suggestion."""
    detection = detect_episode_number(video.filename)
    if detection.version_hint == "Ver.TV":
        return ParserVariantSuggestion("Ver.TV", "TV", release_source="tv")
    if detection.version_hint:
        # In particular UC remains only a label-level hint. The current parser
        # does not prove that it means uncensored in this library.
        return ParserVariantSuggestion(detection.version_hint, detection.version_hint)
    if detection.kind == "structural_variant" and detection.structural_marker:
        return ParserVariantSuggestion(
            detection.structural_marker,
            detection.structural_marker,
        )
    return None


def _load_variant_title(
    session: Session,
    collection_id: int,
    catalog_title_id: int,
) -> CatalogTitle:
    title = session.scalar(select(CatalogTitle).options(
        selectinload(CatalogTitle.video_variant_groups).selectinload(
            VideoVariantGroup.videos
        ),
        selectinload(CatalogTitle.videos).selectinload(Video.video_variant_group),
        selectinload(CatalogTitle.videos).selectinload(Video.duplicate_of),
        selectinload(CatalogTitle.collection).selectinload(CatalogCollection.titles),
        selectinload(CatalogTitle.collection).selectinload(CatalogCollection.videos),
    ).where(
        CatalogTitle.id == catalog_title_id,
        CatalogTitle.catalog_collection_id == collection_id,
    ))
    if title is None or title.collection is None:
        raise ValueError("Video varianty lze spravovat pouze u platné části kolekce.")
    return title


def create_video_variant_group_for_title(
    session: Session,
    collection_id: int,
    catalog_title_id: int,
    *,
    manual_label: str,
    release_source: str | None = None,
    content_variant: str | None = None,
    note: str | None = None,
) -> VideoVariantGroup:
    title = _load_variant_title(session, collection_id, catalog_title_id)
    group = create_video_variant_group(
        title,
        manual_label=manual_label,
        release_source=release_source,
        content_variant=content_variant,
        note=note,
    )
    session.add(group)
    session.flush()
    return group


def update_video_variant_group_for_title(
    session: Session,
    collection_id: int,
    catalog_title_id: int,
    group_id: int,
    *,
    manual_label: str,
    release_source: str | None = None,
    content_variant: str | None = None,
    note: str | None = None,
) -> VideoVariantGroup:
    title = _load_variant_title(session, collection_id, catalog_title_id)
    group = next(
        (item for item in title.video_variant_groups if item.id == group_id),
        None,
    )
    if group is None:
        raise ValueError("Video variant group nebyla v této části nalezena.")
    update_video_variant_group(
        group,
        manual_label=manual_label,
        release_source=release_source,
        content_variant=content_variant,
        note=note,
    )
    session.flush()
    return group


def delete_empty_video_variant_group(
    session: Session,
    collection_id: int,
    catalog_title_id: int,
    group_id: int,
) -> None:
    title = _load_variant_title(session, collection_id, catalog_title_id)
    group = next(
        (item for item in title.video_variant_groups if item.id == group_id),
        None,
    )
    if group is None:
        raise ValueError("Video variant group nebyla v této části nalezena.")
    assigned_count = session.scalar(select(func.count(Video.id)).where(
        Video.video_variant_group_id == group.id
    )) or 0
    if assigned_count:
        raise ValueError(
            "Neprázdnou video variant group nelze odstranit. Nejprve videa "
            "přeřaďte nebo jejich assignment vyčistěte."
        )
    session.delete(group)
    session.flush()


def _normalized_drafts(
    title: CatalogTitle,
    drafts: tuple[VariantGroupDraft, ...],
) -> tuple[VariantGroupDraft, ...]:
    by_key: dict[str, VariantGroupDraft] = {}
    groups_by_id = {group.id: group for group in title.video_variant_groups}
    for draft in drafts:
        key = draft.key.strip()
        if not key or key == "null" or key in by_key:
            raise ValueError("Návrh variant groups obsahuje neplatný nebo duplicitní klíč.")
        if draft.existing_group_id is not None:
            group = groups_by_id.get(draft.existing_group_id)
            if group is None:
                raise ValueError("Vybraná video variant group nepatří k této části.")
            normalized = VariantGroupDraft(
                key=key,
                existing_group_id=group.id,
                manual_label=group.manual_label,
                release_source=group.release_source,
                content_variant=group.content_variant,
                note=group.note,
            )
        else:
            normalized = VariantGroupDraft(
                key=key,
                manual_label=_normalize_manual_label(draft.manual_label),
                release_source=_normalize_taxonomy_value(
                    draft.release_source,
                    VIDEO_VARIANT_RELEASE_SOURCES,
                    field_label="release source",
                ),
                content_variant=_normalize_taxonomy_value(
                    draft.content_variant,
                    VIDEO_VARIANT_CONTENT_VARIANTS,
                    field_label="content variant",
                ),
                note=(draft.note or "").strip() or None,
            )
        by_key[key] = normalized
    return tuple(by_key[key] for key in sorted(by_key))


def _draft_identity(draft: VariantGroupDraft) -> tuple[str, int | str]:
    if draft.existing_group_id is not None:
        return ("group", draft.existing_group_id)
    return ("new", draft.key)


def _draft_display_label(draft: VariantGroupDraft) -> str:
    details = []
    if draft.release_source:
        details.append(f"source {draft.release_source.upper()}")
    if draft.content_variant:
        details.append(draft.content_variant)
    return " · ".join((draft.manual_label, *details))


def _current_assignment_identity(video: Video) -> tuple[str, int] | None:
    return _known_group_identity(video)


def _confirmed_duplicate_components(videos: list[Video]) -> tuple[tuple[Video, ...], ...]:
    by_id = {video.id: video for video in videos if video.id is not None}
    edges: dict[int, set[int]] = {video_id: set() for video_id in by_id}
    for video in videos:
        if (
            video.id is not None
            and video.duplicate_of_video_id is not None
            and video.duplicate_of_video_id in by_id
        ):
            edges[video.id].add(video.duplicate_of_video_id)
            edges[video.duplicate_of_video_id].add(video.id)
    components = []
    visited: set[int] = set()
    for video_id, neighbours in edges.items():
        if video_id in visited or not neighbours:
            continue
        pending = [video_id]
        member_ids = []
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            member_ids.append(current)
            pending.extend(edges[current] - visited)
        components.append(tuple(by_id[item] for item in sorted(member_ids)))
    return tuple(components)


def _validate_prospective_duplicate_groups(
    title: CatalogTitle,
    prospective: dict[int, tuple[str, int | str] | None],
) -> None:
    for component in _confirmed_duplicate_components(list(title.videos)):
        current_identities = {
            _current_assignment_identity(video) for video in component
        } - {None}
        resulting_identities = {
            prospective.get(video.id, _current_assignment_identity(video))
            for video in component
        } - {None}
        if len(resulting_identities) > 1 and (
            len(current_identities) <= 1
            or any(video.id in prospective for video in component)
        ):
            raise ValueError(CONFIRMED_DUPLICATE_VARIANT_CONFLICT_MESSAGE)


def _prospective_collision_counts(
    title: CatalogTitle,
    prospective: dict[int, tuple[str, int | str] | None],
) -> tuple[int, int]:
    by_identity: dict[object, list[Video]] = {}
    for video in title.videos:
        identity = logical_episode_identity(video, catalog_title=title)
        if identity is not None and not is_nonprimary_duplicate_video(video):
            by_identity.setdefault(identity, []).append(video)
    unresolved = 0
    duplicate_collisions = 0
    for videos in by_identity.values():
        if len(videos) < 2:
            continue
        identities = [
            prospective.get(video.id, _current_assignment_identity(video))
            for video in videos
        ]
        if any(identity is None for identity in identities):
            unresolved += 1
            continue
        same_lane = sum(count > 1 for count in Counter(identities).values())
        unresolved += same_lane
        duplicate_collisions += same_lane
    return unresolved, duplicate_collisions


def _assignment_fingerprint(
    title: CatalogTitle,
    workflow: str,
    assignments: tuple[tuple[int, str], ...],
    drafts: tuple[VariantGroupDraft, ...],
) -> str:
    state = [
        (
            video.id,
            video.video_variant_group_id,
            video.catalog_title_id,
            video.season_episode_number,
            video.episode_number_manual_override,
            video.duplicate_of_video_id,
            video.duplicate_primary_missing,
        )
        for video in sorted(title.videos, key=lambda item: item.id or 0)
    ]
    payload = {
        "title": title.id,
        "workflow": workflow,
        "assignments": assignments,
        "drafts": [
            (
                draft.key,
                draft.existing_group_id,
                draft.manual_label,
                draft.release_source,
                draft.content_variant,
                draft.note,
            )
            for draft in drafts
        ],
        "state": state,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def preview_video_variant_assignments(
    session: Session,
    collection_id: int,
    catalog_title_id: int,
    *,
    assignments: tuple[tuple[int, str], ...],
    drafts: tuple[VariantGroupDraft, ...],
    workflow: str = "manual_bulk",
    require_distinct: bool = False,
) -> VariantAssignmentPreview:
    title = _load_variant_title(session, collection_id, catalog_title_id)
    normalized_assignments = tuple(
        sorted((int(video_id), key.strip()) for video_id, key in assignments)
    )
    if not normalized_assignments:
        raise ValueError("Vyberte alespoň jedno video.")
    if len({video_id for video_id, _key in normalized_assignments}) != len(
        normalized_assignments
    ):
        raise ValueError("Jedno video je v návrhu uvedeno vícekrát.")
    normalized_drafts = _normalized_drafts(title, drafts)
    drafts_by_key = {draft.key: draft for draft in normalized_drafts}
    selected_by_id = {
        video.id: video for video in title.videos
        if video.id in {video_id for video_id, _key in normalized_assignments}
    }
    if len(selected_by_id) != len(normalized_assignments):
        raise ValueError("Výběr obsahuje cizí nebo neexistující video.")
    if workflow == "collision":
        collision_identities = {
            logical_episode_identity(video, catalog_title=title)
            for video in selected_by_id.values()
        }
        if (
            len(selected_by_id) < 2
            or len(collision_identities) != 1
            or None in collision_identities
            or any(
                is_nonprimary_duplicate_video(video)
                for video in selected_by_id.values()
            )
        ):
            raise ValueError(
                "Jako různé varianty lze potvrdit pouze aktivní reprezentace "
                "stejné bezpečně známé canonical epizody."
            )

    prospective: dict[int, tuple[str, int | str] | None] = {}
    rows = []
    target_identities = []
    for video_id, key in normalized_assignments:
        video = selected_by_id[video_id]
        if key == "null":
            target_identity = None
            target_label = "neurčeno"
        else:
            draft = drafts_by_key.get(key)
            if draft is None:
                raise ValueError("Návrh odkazuje na chybějící video variant group.")
            target_identity = _draft_identity(draft)
            target_label = _draft_display_label(draft)
        prospective[video_id] = target_identity
        target_identities.append(target_identity)
        current_group = video.video_variant_group
        current_label = (
            _draft_display_label(VariantGroupDraft(
                key="current",
                existing_group_id=current_group.id,
                manual_label=current_group.manual_label,
                release_source=current_group.release_source,
                content_variant=current_group.content_variant,
            ))
            if current_group is not None else "neurčena"
        )
        suggestion = parser_variant_suggestion(video)
        rows.append(VariantAssignmentPreviewRow(
            video_id=video.id,
            filename=video.filename,
            episode_label=(
                f"E{video.season_episode_number:02d}"
                if video.season_episode_number is not None
                else detect_episode_number(video.filename).display_value or "bez canonical čísla"
            ),
            current_label=current_label,
            target_label=target_label,
            parser_hint=suggestion.hint if suggestion else None,
        ))

    expected_distinct_count = (
        2 if workflow == "repeated_lane" else len(target_identities)
    )
    if require_distinct and (
        any(identity is None for identity in target_identities)
        or len(set(target_identities)) != expected_distinct_count
    ):
        raise ValueError(
            "Potvrzení různých variant vyžaduje pro každé video jinou "
            "potvrzenou non-NULL group."
        )
    _validate_prospective_duplicate_groups(title, prospective)
    before, _before_duplicates = _prospective_collision_counts(title, {})
    after, after_duplicates = _prospective_collision_counts(title, prospective)
    new_blockers = (
        ("Výsledný assignment vytvoří novou canonical variant/duplicate kolizi.",)
        if after > before else ()
    )
    fingerprint = _assignment_fingerprint(
        title,
        workflow,
        normalized_assignments,
        normalized_drafts,
    )
    return VariantAssignmentPreview(
        catalog_title_id=title.id,
        workflow=workflow,
        rows=tuple(rows),
        drafts=normalized_drafts,
        assignments=normalized_assignments,
        groups_to_create=tuple(
            draft.manual_label
            for draft in normalized_drafts
            if draft.existing_group_id is None
        ),
        unresolved_collisions_before=before,
        unresolved_collisions_after=after,
        duplicate_collisions_after=after_duplicates,
        new_blockers=new_blockers,
        fingerprint=fingerprint,
    )


def _materialize_variant_assignments(
    session: Session,
    title: CatalogTitle,
    preview: VariantAssignmentPreview,
) -> None:
    groups_by_id = {group.id: group for group in title.video_variant_groups}
    groups_by_key: dict[str, VideoVariantGroup] = {}
    for draft in preview.drafts:
        if draft.existing_group_id is not None:
            groups_by_key[draft.key] = groups_by_id[draft.existing_group_id]
        else:
            group = create_video_variant_group(
                title,
                manual_label=draft.manual_label,
                release_source=draft.release_source,
                content_variant=draft.content_variant,
                note=draft.note,
            )
            session.add(group)
            groups_by_key[draft.key] = group
    session.flush()
    videos_by_id = {video.id: video for video in title.videos}
    selected = [videos_by_id[video_id] for video_id, _key in preview.assignments]
    # Clear first so an otherwise valid whole-group reassignment cannot be
    # rejected because of an in-memory intermediate state.
    for video in selected:
        assign_video_variant_group(video, None)
    for video_id, key in preview.assignments:
        assign_video_variant_group(
            videos_by_id[video_id],
            None if key == "null" else groups_by_key[key],
        )
    session.flush()


def apply_video_variant_assignments(
    session: Session,
    collection_id: int,
    catalog_title_id: int,
    *,
    assignments: tuple[tuple[int, str], ...],
    drafts: tuple[VariantGroupDraft, ...],
    expected_fingerprint: str,
    workflow: str = "manual_bulk",
    require_distinct: bool = False,
) -> VariantAssignmentPreview:
    from .hierarchy_evaluation import finalize_hierarchy_write

    preview = preview_video_variant_assignments(
        session,
        collection_id,
        catalog_title_id,
        assignments=assignments,
        drafts=drafts,
        workflow=workflow,
        require_distinct=require_distinct,
    )
    if not expected_fingerprint or preview.fingerprint != expected_fingerprint:
        raise ValueError(
            "Náhled variant už neodpovídá aktuálnímu stavu. Načtěte jej znovu."
        )
    title = _load_variant_title(session, collection_id, catalog_title_id)
    numbering_before = {
        video.id: (
            video.local_episode_number,
            video.season_episode_number,
            video.absolute_episode_number,
            video.external_episode_number,
            video.episode_number_manual_override,
        )
        for video in title.videos
    }
    duplicate_before = {
        video.id: (video.duplicate_of_video_id, video.duplicate_primary_missing)
        for video in title.videos
    }
    hierarchy_before = {
        video.id: (video.catalog_title_id, video.catalog_collection_id)
        for video in title.collection.videos
    }
    _materialize_variant_assignments(session, title, preview)
    finalize_hierarchy_write([title.collection], recalculate=False)
    session.flush()
    if numbering_before != {
        video.id: (
            video.local_episode_number,
            video.season_episode_number,
            video.absolute_episode_number,
            video.external_episode_number,
            video.episode_number_manual_override,
        )
        for video in title.videos
    }:
        raise ValueError("Variant assignment nesmí měnit canonical numbering.")
    if duplicate_before != {
        video.id: (video.duplicate_of_video_id, video.duplicate_primary_missing)
        for video in title.videos
    }:
        raise ValueError("Variant assignment nesmí měnit duplicate vztahy.")
    if hierarchy_before != {
        video.id: (video.catalog_title_id, video.catalog_collection_id)
        for video in title.collection.videos
    }:
        raise ValueError("Variant assignment nesmí měnit hierarchy membership.")
    return preview


def _proposal_fingerprint(kind: str, title: CatalogTitle, rows: list[tuple]) -> str:
    payload = {
        "kind": kind,
        "title": title.id,
        "rows": rows,
        "videos": [
            (
                video.id,
                video.catalog_title_id,
                video.video_variant_group_id,
                video.season_episode_number,
                video.episode_number_manual_override,
                video.duplicate_of_video_id,
                detect_episode_number(video.filename).kind,
                detect_episode_number(video.filename).number,
                detect_episode_number(video.filename).version_hint,
                detect_episode_number(video.filename).structural_marker,
            )
            for video in sorted(title.videos, key=lambda item: item.id or 0)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def repeated_variant_lane_proposal(
    title: CatalogTitle,
) -> VariantLaneProposal | None:
    """Detect one conservative repeated hint/plain canonical collision pattern."""
    if any(
        video.duplicate_of_video_id is not None or video.duplicate_copies
        for video in title.videos
    ):
        return None
    by_identity: dict[object, list[Video]] = {}
    for video in title.videos:
        identity = logical_episode_identity(video, catalog_title=title)
        if identity is not None and not is_nonprimary_duplicate_video(video):
            by_identity.setdefault(identity, []).append(video)
    pairs: list[VariantLanePair] = []
    proposal_hint: str | None = None
    for identity, videos in sorted(
        by_identity.items(),
        key=lambda item: item[0].season_episode_number,
    ):
        if len(videos) < 2:
            continue
        current = [_current_assignment_identity(video) for video in videos]
        currently_unresolved = (
            any(value is None for value in current)
            or len(set(current)) != len(current)
        )
        if not currently_unresolved:
            continue
        if len(videos) != 2:
            return None
        hinted = [
            video for video in videos
            if detect_episode_number(video.filename).version_hint is not None
        ]
        plain = [
            video for video in videos
            if detect_episode_number(video.filename).version_hint is None
        ]
        if len(hinted) != 1 or len(plain) != 1:
            return None
        hint = detect_episode_number(hinted[0].filename).version_hint
        if hint is None or proposal_hint not in {None, hint}:
            return None
        proposal_hint = hint
        pairs.append(VariantLanePair(
            episode_number=identity.season_episode_number,
            hinted_video_id=hinted[0].id,
            hinted_filename=hinted[0].filename,
            plain_video_id=plain[0].id,
            plain_filename=plain[0].filename,
        ))
    if proposal_hint is None or len(pairs) < 2:
        return None
    suggestion = parser_variant_suggestion(next(
        video for video in title.videos if video.id == pairs[0].hinted_video_id
    ))
    assert suggestion is not None
    rows = [
        (
            pair.episode_number,
            pair.hinted_video_id,
            pair.plain_video_id,
        )
        for pair in pairs
    ]
    return VariantLaneProposal(
        catalog_title_id=title.id,
        parser_hint=proposal_hint,
        pairs=tuple(pairs),
        hinted_suggestion=suggestion,
        fingerprint=_proposal_fingerprint("repeated_lane", title, rows),
    )


def preview_repeated_variant_lane(
    session: Session,
    collection_id: int,
    catalog_title_id: int,
    *,
    hinted_draft: VariantGroupDraft,
    plain_draft: VariantGroupDraft,
    expected_proposal_fingerprint: str,
) -> VariantAssignmentPreview:
    title = _load_variant_title(session, collection_id, catalog_title_id)
    proposal = repeated_variant_lane_proposal(title)
    if proposal is None or proposal.fingerprint != expected_proposal_fingerprint:
        raise ValueError(
            "Hromadný návrh už neodpovídá bezpečnému aktuálnímu patternu."
        )
    assignments = tuple(
        item
        for pair in proposal.pairs
        for item in (
            (pair.hinted_video_id, hinted_draft.key),
            (pair.plain_video_id, plain_draft.key),
        )
    )
    return preview_video_variant_assignments(
        session,
        collection_id,
        catalog_title_id,
        assignments=assignments,
        drafts=(hinted_draft, plain_draft),
        workflow="repeated_lane",
        require_distinct=True,
    )


def structural_ab_pair_proposals(
    title: CatalogTitle,
) -> tuple[StructuralABPairProposal, ...]:
    by_number: dict[int, list[tuple[Video, str]]] = {}
    for video in title.videos:
        detection = detect_episode_number(video.filename)
        if (
            detection.kind == "structural_variant"
            and detection.number is not None
            and detection.structural_marker in {"A", "B"}
        ):
            by_number.setdefault(detection.number, []).append(
                (video, detection.structural_marker)
            )
    proposals = []
    for number, candidates in sorted(by_number.items()):
        if len(candidates) != 2 or {marker for _video, marker in candidates} != {"A", "B"}:
            continue
        selected = {video.id for video, _marker in candidates}
        conflicting = False
        for video in title.videos:
            detection = detect_episode_number(video.filename)
            if video.id not in selected and (
                (detection.kind == "standard" and detection.number == number)
                or (
                    detection.kind == "structural_variant"
                    and detection.number == number
                )
                or video.season_episode_number == number
                or video.episode_number_manual_override == number
            ):
                conflicting = True
                break
        if conflicting or any(
            video.episode_number_manual_override not in {None, number}
            or video.content_type_manual is not None
            or video.duplicate_of_video_id is not None
            or video.duplicate_copies
            for video, _marker in candidates
        ):
            continue
        by_marker = {marker: video for video, marker in candidates}
        rows = [(number, by_marker["A"].id, by_marker["B"].id)]
        proposals.append(StructuralABPairProposal(
            catalog_title_id=title.id,
            episode_number=number,
            video_a_id=by_marker["A"].id,
            filename_a=by_marker["A"].filename,
            video_b_id=by_marker["B"].id,
            filename_b=by_marker["B"].filename,
            fingerprint=_proposal_fingerprint("structural_ab", title, rows),
        ))
    return tuple(proposals)


def preview_structural_ab_confirmation(
    session: Session,
    collection_id: int,
    catalog_title_id: int,
    *,
    video_a_id: int,
    video_b_id: int,
    draft_a: VariantGroupDraft,
    draft_b: VariantGroupDraft,
    expected_proposal_fingerprint: str,
) -> StructuralABPreview:
    title = _load_variant_title(session, collection_id, catalog_title_id)
    proposal = next((
        item for item in structural_ab_pair_proposals(title)
        if item.video_a_id == video_a_id and item.video_b_id == video_b_id
    ), None)
    if proposal is None or proposal.fingerprint != expected_proposal_fingerprint:
        raise ValueError("A/B návrh už neodpovídá aktuálnímu bezpečnému stavu.")
    assignment_preview = preview_video_variant_assignments(
        session,
        collection_id,
        catalog_title_id,
        assignments=((video_a_id, draft_a.key), (video_b_id, draft_b.key)),
        drafts=(draft_a, draft_b),
        workflow="structural_ab",
        require_distinct=True,
    )
    return StructuralABPreview(proposal, assignment_preview)


def apply_structural_ab_confirmation(
    session: Session,
    collection_id: int,
    catalog_title_id: int,
    *,
    video_a_id: int,
    video_b_id: int,
    draft_a: VariantGroupDraft,
    draft_b: VariantGroupDraft,
    expected_proposal_fingerprint: str,
    expected_assignment_fingerprint: str,
) -> StructuralABPreview:
    from .hierarchy_evaluation import finalize_hierarchy_write

    preview = preview_structural_ab_confirmation(
        session,
        collection_id,
        catalog_title_id,
        video_a_id=video_a_id,
        video_b_id=video_b_id,
        draft_a=draft_a,
        draft_b=draft_b,
        expected_proposal_fingerprint=expected_proposal_fingerprint,
    )
    if preview.assignment_preview.fingerprint != expected_assignment_fingerprint:
        raise ValueError(
            "A/B náhled už neodpovídá aktuálnímu stavu. Načtěte jej znovu."
        )
    title = _load_variant_title(session, collection_id, catalog_title_id)
    videos_by_id = {video.id: video for video in title.videos}
    set_video_episode_override(videos_by_id[video_a_id], preview.proposal.episode_number)
    set_video_episode_override(videos_by_id[video_b_id], preview.proposal.episode_number)
    _materialize_variant_assignments(session, title, preview.assignment_preview)
    finalize_hierarchy_write([title.collection], recalculate=True)
    session.flush()
    first, second = videos_by_id[video_a_id], videos_by_id[video_b_id]
    if (
        first.season_episode_number != preview.proposal.episode_number
        or second.season_episode_number != preview.proposal.episode_number
        or first.video_variant_group_id is None
        or second.video_variant_group_id is None
        or first.video_variant_group_id == second.video_variant_group_id
    ):
        raise ValueError("A/B potvrzení nevytvořilo očekávaný atomický stav.")
    return preview
