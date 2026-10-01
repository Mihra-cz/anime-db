"""Batch evidence loading and explicit human writes with caller-owned commit."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from types import MappingProxyType
from typing import Iterable

from sqlalchemy import inspect, or_, select
from sqlalchemy.orm import Session, joinedload, object_session, selectinload

from .catalog import effective_video_content_type
from .hierarchy_authority import manual_hierarchy_authority_state, structural_hierarchy_issue
from .metadata.completion import has_confirmed_metadata
from .metadata.link_lifecycle import confirmed_primary_external_link
from .models import CatalogCollection, CatalogTitle, PhysicalLayoutChoice, Video
from .numbering import is_confirmed_duplicate
from .physical_layout import (
    LAYOUT_TITLE_TYPES, LayoutAttachment, LayoutChoice, LayoutTitle, PhysicalLayoutContext,
    create_basis_snapshot,
)
from .physical_layout_types import PHYSICAL_LAYOUT_KINDS
from .supplementary import supplementary_inventory


def _safe_logical_count(videos: list[Video], title: CatalogTitle) -> int | None:
    inventory = supplementary_inventory(videos, title)
    count = inventory.logical_identity_count
    if count is None:
        return None
    # The shared inventory folds valid duplicates, variants and complete MP.
    # Duplicate evidence that did not fold away is unresolved: the copy is
    # neither another identity nor silently subtracted.
    active = [video for partition in inventory.partitions for video in partition.videos]
    if any(is_confirmed_duplicate(video) for video in (*active, *inventory.unknown_videos)):
        return None
    # Its multiplicity count also includes unidentified physical items; those
    # cannot prove another identity alongside the same effective subtype.
    unnumbered_by_type: dict[str, list[tuple[Video, ...]]] = {}
    for group in inventory.unnumbered_identity_groups:
        kind = effective_video_content_type(group[0], title, use_current_title=False)
        unnumbered_by_type.setdefault(kind, []).append(group)
    known_types = {partition.identity.supplementary_type for partition in inventory.partitions}
    if any(len(groups) > 1 or kind in known_types
           for kind, groups in unnumbered_by_type.items()):
        # An unnumbered physical row can represent an already known item.
        # Neither its filename nor provider episode count proves separation.
        return None
    return count


def load_physical_layout_context(
    session: Session, *, collection_ids: Iterable[int] | None = None,
    title_ids: Iterable[int] = (),
) -> PhysicalLayoutContext:
    """Preload the identity graph in bounded batches, without autoflush or I/O."""
    query = select(CatalogTitle).options(
        joinedload(CatalogTitle.collection).selectinload(CatalogCollection.titles),
        selectinload(CatalogTitle.external_links),
        selectinload(CatalogTitle.physical_layout_choice),
        selectinload(CatalogTitle.videos).joinedload(Video.catalog_title).joinedload(CatalogTitle.collection),
        selectinload(CatalogTitle.videos).joinedload(Video.duplicate_of).joinedload(Video.catalog_title).joinedload(CatalogTitle.collection),
    )
    if collection_ids is not None:
        query = query.where(or_(
            CatalogTitle.catalog_collection_id.in_(tuple(collection_ids)),
            CatalogTitle.id.in_(tuple(title_ids)),
        ))
    with session.no_autoflush:
        titles = list(session.scalars(query))
        choices = [title.physical_layout_choice for title in titles
                   if title.physical_layout_choice is not None and title.physical_layout_choice not in session.deleted]
        ids = {title.id for title in titles}
        choices.extend(choice for choice in session.new
                       if isinstance(choice, PhysicalLayoutChoice) and choice.catalog_title_id in ids)
        return physical_layout_context_from_models(titles, choices)


def physical_layout_context_from_models(titles, choices) -> PhysicalLayoutContext:
    """Project a preloaded graph once; the resolver consumes immutable scalars."""
    title_models = {}
    for title in titles:
        identity = None
        if has_confirmed_metadata(title):
            link = confirmed_primary_external_link(title)
            identity = (link.provider, link.external_id)
        structural_valid = (
            title.catalog_collection_id is not None
            and manual_hierarchy_authority_state(title) != "incomplete"
            and structural_hierarchy_issue(title.effective_part_type, title.effective_season_number,
                                           title.effective_part_number) is None
        )
        season = title.effective_season_number
        attachment = LayoutAttachment("root" if season is None else "season", season)
        videos = list(title.videos)
        types = tuple(sorted({effective_video_content_type(video, title, use_current_title=False)
                              for video in videos}))
        # IV is existing release evidence, not a new content classification.
        # Reduce it to a stable profile flag, never persist filename/title text.
        interview = bool(re.search(r"\binterviews?\b", title.local_title, re.I)) or any(
            re.search(r"(?:\[|\b)IV\d+(?:\]|\b)", video.filename, re.I) for video in videos
        )
        interpretation = "typed_content"
        if (title.effective_part_type == "preview"
            and types == ("preview",) and manual_hierarchy_authority_state(title) == "complete"):
            # A manual Preview container in its authoritative Season is the
            # approved story-preview contract. Automatic Preview/PV evidence
            # alone cannot establish that chronological role.
            interpretation = "story_preview"
        count = _safe_logical_count(videos, title)
        if any(kind in {"episode", "film", "other"} for kind in types):
            count = None
        title_models[title.id] = LayoutTitle(
            title.id, title.catalog_collection_id, title.effective_part_type,
            attachment, structural_valid, types, interpretation,
            identity, count, interview,
        )
    choice_models = {choice.catalog_title_id: LayoutChoice(
        choice.id, choice.layout_kind, choice.confirmed_at, choice.basis_snapshot_json,
    ) for choice in choices}
    return PhysicalLayoutContext(MappingProxyType(title_models), MappingProxyType(choice_models))


def _owner_id(session: Session, owner: CatalogTitle) -> int:
    if (not isinstance(owner, CatalogTitle) or object_session(owner) is not session
        or not inspect(owner).persistent or owner in session.deleted):
        raise ValueError("Layout owner must be a persisted CatalogTitle in the caller's session.")
    with session.no_autoflush:
        return owner.id


def _find_choice(session: Session, title_id: int) -> PhysicalLayoutChoice | None:
    for choice in session.new:
        if isinstance(choice, PhysicalLayoutChoice) and choice.catalog_title_id == title_id:
            return choice
    with session.no_autoflush:
        return session.scalar(select(PhysicalLayoutChoice).where(PhysicalLayoutChoice.catalog_title_id == title_id))


def confirm_physical_layout_choice(
    session: Session, owner: CatalogTitle, layout_kind: str, *, now: datetime | None = None,
) -> PhysicalLayoutChoice:
    if layout_kind not in PHYSICAL_LAYOUT_KINDS:
        raise ValueError("Unknown physical layout kind.")
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Layout confirmation timestamp must be timezone-aware.")
    title_id = _owner_id(session, owner)
    with session.no_autoflush:
        if owner.effective_part_type not in LAYOUT_TITLE_TYPES:
            raise ValueError("Physical Layout choices belong only to supplementary CatalogTitles, excluding films.")
        context = load_physical_layout_context(
            session, collection_ids=[owner.catalog_collection_id], title_ids=[title_id],
        )
        basis = create_basis_snapshot(context, title_id)
        choice = _find_choice(session, title_id)
        if choice is None:
            choice = PhysicalLayoutChoice(catalog_title_id=title_id)
            session.add(choice)
        elif choice in session.deleted:
            session.add(choice)
        choice.layout_kind = layout_kind
        choice.confirmed_at = timestamp.astimezone(timezone.utc)
        choice.basis_snapshot_json = basis
        session.expire(owner, ["physical_layout_choice"])
        return choice


def reconfirm_physical_layout_choice(
    session: Session, owner: CatalogTitle, *, now: datetime | None = None,
) -> PhysicalLayoutChoice:
    choice = _find_choice(session, _owner_id(session, owner))
    if choice is None or choice in session.deleted:
        raise ValueError("There is no physical layout choice to reconfirm.")
    return confirm_physical_layout_choice(session, owner, choice.layout_kind, now=now)


def reset_physical_layout_choice(session: Session, owner: CatalogTitle) -> None:
    choice = _find_choice(session, _owner_id(session, owner))
    if choice is not None:
        if inspect(choice).pending:
            session.expunge(choice)
        else:
            session.delete(choice)
        session.expire(owner, ["physical_layout_choice"])
