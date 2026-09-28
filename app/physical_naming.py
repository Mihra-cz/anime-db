"""Pure unsanitized physical-name projection over a preloaded naming context.

No display fallback, filesystem formatting, or length-based Review policy lives
here. A saved choice remains visible when its confirmation basis becomes stale.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Mapping

from .hierarchy_types import MAIN_CONTENT_PART_TYPES, SUPPLEMENTARY_PART_TYPES


@dataclass(frozen=True)
class NamingTitle:
    id: int
    collection_id: int | None
    part_type: str
    season_number: int | None
    part_number: int | None
    structural_valid: bool
    metadata_identity: tuple[str, str] | None
    romaji: str | None
    metadata_diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class NamingChoice:
    id: int | None
    physical_text: str
    choice_kind: str
    confirmed_at: datetime
    basis_snapshot_json: str


@dataclass(frozen=True)
class PhysicalNamingContext:
    collections: Mapping[int, tuple[int, ...]]
    titles: Mapping[int, NamingTitle]
    choices: Mapping[tuple[str, int], NamingChoice]


@dataclass(frozen=True)
class PhysicalNameResolution:
    scope: str
    owner_id: int
    choice: NamingChoice | None
    effective_text: str | None
    authority: str
    default_candidate: str | None
    dependency: tuple[str, int] | None = None
    basis_matches: bool | None = None
    diagnostics: tuple[str, ...] = ()
    metadata_source_title_id: int | None = None
    metadata_identity: tuple[str, str] | None = None

    @property
    def ready(self) -> bool:
        """Resolved text/basis only; this is not filesystem/planner readiness."""
        return bool(self.effective_text) and not self.diagnostics


def _root_source(context: PhysicalNamingContext, collection_id: int) -> tuple[NamingTitle | None, tuple[NamingTitle, ...]]:
    titles = tuple(context.titles[id] for id in context.collections[collection_id])
    mains = tuple(title for title in titles if title.part_type in MAIN_CONTENT_PART_TYPES)
    candidates = (
        tuple(title for title in mains if title.season_number == 1 and title.part_number in (None, 1))
        if mains else tuple(title for title in titles if title.part_type == "film")
    )
    source = candidates[0] if (
        len(candidates) == 1 and candidates[0].structural_valid
        and candidates[0].metadata_identity is not None
    ) else None
    # Ambiguous context records evidence, never a newly chosen metadata anchor.
    evidence = candidates if source is not None else (mains or candidates)
    return source, evidence


def _parents(context: PhysicalNamingContext, title: NamingTitle) -> tuple[NamingTitle, ...]:
    if title.collection_id is None or title.season_number is None:
        return ()
    return tuple(
        parent for id in context.collections[title.collection_id]
        if (parent := context.titles[id]).part_type in MAIN_CONTENT_PART_TYPES
        and parent.season_number == title.season_number
        and (title.part_number is None or parent.part_number == title.part_number)
    )


def _metadata_snapshot(state: str, titles: tuple[NamingTitle, ...]) -> dict:
    identities = sorted({title.metadata_identity for title in titles if title.metadata_identity is not None})
    return {"state": state, "identities": [
        {"provider": provider, "external_id": external_id} for provider, external_id in identities
    ]}


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_basis_snapshot(
    context: PhysicalNamingContext, scope: str, owner_id: int, *, source_title_id: int | None = None,
) -> str:
    """Serialize current identity/context; text and refresh timestamps are absent."""
    structure = None
    if scope == "collection":
        source, evidence = _root_source(context, owner_id)
        metadata = _metadata_snapshot("confirmed" if source else "ambiguous", evidence)
    elif scope == "title":
        title = context.titles[owner_id]
        metadata = _metadata_snapshot("confirmed" if title.metadata_identity else "none", (title,))
        if title.metadata_identity is None and title.part_type in SUPPLEMENTARY_PART_TYPES:
            structure = {
                "collection_id": title.collection_id, "part_type": title.part_type,
                "season_number": title.season_number, "part_number": title.part_number,
                "parent_title_ids": sorted(parent.id for parent in _parents(context, title)),
            }
    else:
        raise ValueError("Unknown physical naming scope.")
    return _canonical_json({
        "version": 1, "owner": {"scope": scope, "id": owner_id},
        "metadata": metadata, "structure": structure, "source_title_id": source_title_id,
    })


def evaluate_basis_match(context: PhysicalNamingContext, scope: str, owner_id: int, choice: NamingChoice) -> tuple[bool, tuple[str, ...]]:
    try:
        saved = json.loads(choice.basis_snapshot_json)
        current = json.loads(create_basis_snapshot(context, scope, owner_id))
        # Compare JSON encodings: Python equality would accept true or 1.0 as 1.
        if (
            not isinstance(saved, dict) or set(saved) != set(current)
            or _canonical_json(saved["version"]) != "1"
            or _canonical_json(saved["owner"]) != _canonical_json(current["owner"])
        ):
            return False, ("invalid_basis_snapshot",)
        diagnostics = []
        if _canonical_json(saved["metadata"]) != _canonical_json(current["metadata"]):
            diagnostics.append("metadata_identity_changed")
        if _canonical_json(saved["structure"]) != _canonical_json(current["structure"]):
            diagnostics.append("structural_context_changed")
        # source_title_id is historical provenance, not a live reference.
        return not diagnostics, tuple(diagnostics)
    except (ValueError, TypeError):
        return False, ("invalid_basis_snapshot",)


def resolve_physical_name(
    context: PhysicalNamingContext, scope: str, owner_id: int,
) -> PhysicalNameResolution:
    default = None
    dependency = None
    diagnostics: tuple[str, ...] = ()
    authority = "unavailable"
    metadata_source_title_id = None
    metadata_identity = None
    if scope == "collection":
        source, _ = _root_source(context, owner_id)
        if source is None:
            authority, diagnostics = "ambiguous", ("authority_ambiguous",)
        else:
            metadata_source_title_id = source.id
            metadata_identity = source.metadata_identity
            default = source.romaji
            diagnostics = source.metadata_diagnostics
    elif scope == "title":
        title = context.titles[owner_id]
        if title.metadata_identity is not None:
            metadata_source_title_id = title.id
            metadata_identity = title.metadata_identity
            default = title.romaji
            diagnostics = title.metadata_diagnostics
        elif title.part_type in SUPPLEMENTARY_PART_TYPES:
            if not title.structural_valid or title.collection_id is None:
                diagnostics = ("structural_context_unavailable",)
            elif title.season_number is None:
                dependency = ("collection", title.collection_id)
            else:
                parents = _parents(context, title)
                if len(parents) == 1 and parents[0].structural_valid:
                    dependency = ("title", parents[0].id)
                elif len(parents) > 1:
                    authority, diagnostics = "ambiguous", ("season_only_multiple_parts",)
                else:
                    diagnostics = ("structural_context_unavailable",)
        else:
            diagnostics = ("confirmed_metadata_unavailable",)
    else:
        raise ValueError("Unknown physical naming scope.")
    if default is not None and not diagnostics:
        authority = "derived_default"
    elif not diagnostics and dependency is None:
        diagnostics = ("confirmed_romaji_unavailable",)

    choice = context.choices.get((scope, owner_id))
    if choice is not None:
        matches, basis_diagnostics = evaluate_basis_match(context, scope, owner_id, choice)
        # Human text closes missing/ambiguous defaults, without granting an anchor.
        return PhysicalNameResolution(
            scope, owner_id, choice, choice.physical_text, "human_choice", default,
            basis_matches=matches, diagnostics=basis_diagnostics,
            metadata_source_title_id=metadata_source_title_id, metadata_identity=metadata_identity,
        )
    if dependency is not None:
        parent = resolve_physical_name(context, *dependency)
        return PhysicalNameResolution(
            scope, owner_id, None, parent.effective_text, "inherited", parent.effective_text,
            dependency=dependency, diagnostics=() if parent.ready else ("dependency_unresolved",),
            metadata_source_title_id=parent.metadata_source_title_id, metadata_identity=parent.metadata_identity,
        )
    return PhysicalNameResolution(
        scope, owner_id, None, default, authority, default, diagnostics=diagnostics,
        metadata_source_title_id=metadata_source_title_id, metadata_identity=metadata_identity,
    )
