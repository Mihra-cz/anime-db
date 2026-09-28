"""Explicit naming writes and batch loading; transaction ownership stays with caller."""
from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Iterable
import unicodedata

from sqlalchemy import inspect, or_, select
from sqlalchemy.orm import Session, object_session, selectinload

from .hierarchy_authority import manual_hierarchy_authority_state, structural_hierarchy_issue
from .metadata.completion import has_confirmed_metadata
from .metadata.link_lifecycle import confirmed_primary_external_link
from .models import CatalogCollection, CatalogTitle, PhysicalNamingChoice
from .physical_naming import (
    NamingChoice, NamingTitle, PhysicalNamingContext, create_basis_snapshot, resolve_physical_name,
)
from .physical_naming_types import MAX_PHYSICAL_TEXT_LENGTH, PHYSICAL_NAMING_CHOICE_KINDS


def validate_physical_text(value: str) -> str:
    if not isinstance(value, str) or any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in value):
        raise ValueError("Physical naming text cannot contain controls, line breaks or surrogates.")
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_PHYSICAL_TEXT_LENGTH:
        raise ValueError("Physical naming text must contain 1–500 Unicode characters.")
    return normalized


def load_physical_naming_context(
    session: Session, *, collection_ids: Iterable[int] | None = None, title_ids: Iterable[int] = (),
) -> PhysicalNamingContext:
    """Load scalar read models in batches; pure resolvers perform no ORM lookups."""
    collections_query = select(CatalogCollection)
    titles_query = select(CatalogTitle).options(
        selectinload(CatalogTitle.metadata_record), selectinload(CatalogTitle.external_links),
    )
    choice_query = select(PhysicalNamingChoice)
    if collection_ids is not None:
        selected_collections = tuple(collection_ids)
        selected_titles = tuple(title_ids)
        collections_query = collections_query.where(CatalogCollection.id.in_(selected_collections))
        titles_query = titles_query.where(or_(
            CatalogTitle.catalog_collection_id.in_(selected_collections), CatalogTitle.id.in_(selected_titles),
        ))
        choice_query = choice_query.where(or_(
            PhysicalNamingChoice.catalog_collection_id.in_(selected_collections),
            PhysicalNamingChoice.catalog_title_id.in_(titles_query.with_only_columns(CatalogTitle.id).options()),
        ))
    with session.no_autoflush:
        collections = list(session.scalars(collections_query))
        titles = list(session.scalars(titles_query))
        choices = list(session.scalars(choice_query))
    # Include the caller's unflushed naming decisions without flushing any
    # unrelated domain edits. Reset decisions are immediately absent as well.
    choices = [choice for choice in choices if choice not in session.deleted]
    loaded_collection_ids = {owner.id for owner in collections}
    loaded_title_ids = {title.id for title in titles}
    choices.extend(
        choice for choice in session.new if isinstance(choice, PhysicalNamingChoice)
        and (choice.catalog_collection_id in loaded_collection_ids
             or choice.catalog_title_id in loaded_title_ids)
    )
    return physical_naming_context_from_models(collections, titles, choices)


def physical_naming_context_from_models(collections, titles, choices) -> PhysicalNamingContext:
    """Project an already preloaded ORM graph; callers own loading and writes."""
    title_models = {}
    for title in titles:
        identity = None
        romaji = None
        diagnostics = ()
        if has_confirmed_metadata(title):
            link = confirmed_primary_external_link(title)
            identity = (link.provider, link.external_id)
            metadata = title.metadata_record
            if metadata is None or (metadata.metadata_provider, metadata.metadata_external_id) != identity:
                diagnostics = ("metadata_payload_mismatch",)
            else:
                raw = metadata.title_romaji
                if raw:
                    try:
                        romaji = validate_physical_text(raw)
                    except ValueError:
                        diagnostics = ("invalid_default_text",)
                if not romaji and not diagnostics:
                    diagnostics = ("confirmed_romaji_unavailable",)
        structural_valid = (
            manual_hierarchy_authority_state(title) != "incomplete"
            and structural_hierarchy_issue(title.effective_part_type, title.effective_season_number, title.effective_part_number) is None
        )
        title_models[title.id] = NamingTitle(
            title.id, title.catalog_collection_id, title.effective_part_type,
            title.effective_season_number, title.effective_part_number,
            structural_valid, identity, romaji, diagnostics,
        )
    collection_models: dict[int, list[int]] = {owner.id: [] for owner in collections}
    for title in titles:
        if title.catalog_collection_id in collection_models:
            collection_models[title.catalog_collection_id].append(title.id)
    choice_models = {}
    for choice in choices:
        key = ("collection", choice.catalog_collection_id) if choice.catalog_collection_id is not None else ("title", choice.catalog_title_id)
        choice_models[key] = NamingChoice(
            choice.id, choice.physical_text, choice.choice_kind, choice.confirmed_at, choice.basis_snapshot_json,
        )
    return PhysicalNamingContext(
        MappingProxyType({id: tuple(sorted(ids)) for id, ids in collection_models.items()}),
        MappingProxyType(title_models), MappingProxyType(choice_models),
    )


def _owner_key(session: Session, owner: CatalogCollection | CatalogTitle) -> tuple[str, int]:
    if (not isinstance(owner, (CatalogCollection, CatalogTitle))
        or object_session(owner) is not session or not inspect(owner).persistent
        or owner in session.deleted):
        raise ValueError("Naming owner must be persisted in the caller's session.")
    with session.no_autoflush:
        return ("collection" if isinstance(owner, CatalogCollection) else "title", owner.id)


def _choice_query(scope: str, owner_id: int):
    fk = PhysicalNamingChoice.catalog_collection_id if scope == "collection" else PhysicalNamingChoice.catalog_title_id
    return select(PhysicalNamingChoice).where(fk == owner_id)


def _find_choice(session: Session, scope: str, owner_id: int) -> PhysicalNamingChoice | None:
    field = "catalog_collection_id" if scope == "collection" else "catalog_title_id"
    for choice in session.new:
        if isinstance(choice, PhysicalNamingChoice) and getattr(choice, field) == owner_id:
            return choice
    with session.no_autoflush:
        return session.scalar(_choice_query(scope, owner_id))


def confirm_physical_naming_choice(
    session: Session, owner: CatalogCollection | CatalogTitle, physical_text: str, choice_kind: str,
    *, source_title_id: int | None = None, now: datetime | None = None,
) -> PhysicalNamingChoice:
    text = validate_physical_text(physical_text)
    if choice_kind not in PHYSICAL_NAMING_CHOICE_KINDS:
        raise ValueError("Unknown physical naming choice kind.")
    scope, owner_id = _owner_key(session, owner)
    if choice_kind != "parent_prefix" and source_title_id is not None:
        raise ValueError("Source title provenance is only valid for parent_prefix.")
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Naming confirmation timestamp must be timezone-aware.")
    with session.no_autoflush:
        collections = [owner_id] if scope == "collection" else (
            [owner.catalog_collection_id] if owner.catalog_collection_id is not None else []
        )
    context = load_physical_naming_context(session, collection_ids=collections, title_ids=[owner_id] if scope == "title" else [])
    basis = create_basis_snapshot(context, scope, owner_id, source_title_id=source_title_id)
    if choice_kind == "parent_prefix":
        import json
        structure = json.loads(basis)["structure"]
        if structure is None or source_title_id not in structure["parent_title_ids"]:
            raise ValueError("Choose a relevant parent title for this supplementary context.")
        parent = resolve_physical_name(context, "title", source_title_id)
        if not parent.ready or parent.effective_text != text:
            raise ValueError("parent_prefix must copy the currently resolved parent text.")
    choice = _find_choice(session, scope, owner_id)
    if choice is None:
        choice = PhysicalNamingChoice(**{
            "catalog_collection_id" if scope == "collection" else "catalog_title_id": owner_id,
        })
        session.add(choice)
    elif choice in session.deleted:
        session.add(choice)
    choice.physical_text = text
    choice.choice_kind = choice_kind
    choice.confirmed_at = timestamp.astimezone(timezone.utc)
    choice.basis_snapshot_json = basis
    session.expire(owner, ["physical_naming_choice"])
    return choice


def reset_physical_naming_choice(session: Session, owner: CatalogCollection | CatalogTitle) -> None:
    scope, owner_id = _owner_key(session, owner)
    choice = _find_choice(session, scope, owner_id)
    if choice is not None:
        if inspect(choice).pending:
            session.expunge(choice)
        else:
            session.delete(choice)
        session.expire(owner, ["physical_naming_choice"])


def reconfirm_physical_naming_choice(
    session: Session, owner: CatalogCollection | CatalogTitle, *, now: datetime | None = None,
) -> PhysicalNamingChoice:
    """Explicitly retain a historical snapshot while approving current context."""
    import json
    scope, owner_id = _owner_key(session, owner)
    choice = _find_choice(session, scope, owner_id)
    if choice is None:
        raise ValueError("There is no physical naming choice to reconfirm.")
    if choice.choice_kind != "parent_prefix":
        return confirm_physical_naming_choice(session, owner, choice.physical_text, choice.choice_kind, now=now)
    validate_physical_text(choice.physical_text)
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Naming confirmation timestamp must be timezone-aware.")
    try:
        source = json.loads(choice.basis_snapshot_json).get("source_title_id")
    except (ValueError, AttributeError):
        source = None
    source = source if type(source) is int and source > 0 else None
    context = load_physical_naming_context(
        session, collection_ids=[owner.catalog_collection_id] if owner.catalog_collection_id is not None else [],
        title_ids=[owner_id],
    )
    # This is historical provenance, not a fresh selection of a parent candidate.
    # A parent's later rename must not rewrite the explicitly retained snapshot.
    choice.basis_snapshot_json = create_basis_snapshot(context, scope, owner_id, source_title_id=source)
    choice.confirmed_at = timestamp.astimezone(timezone.utc)
    session.expire(owner, ["physical_naming_choice"])
    return choice
