"""Pure grouping resolution over immutable, batch-loaded scalar evidence.

Naming supplies future folder components; hierarchy supplies attachment. This
module supplies neither paths nor new identity and never authors human choices.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Mapping

from .hierarchy_types import SUPPLEMENTARY_PART_TYPES
from .physical_layout_types import PHYSICAL_LAYOUT_KINDS


LAYOUT_TITLE_TYPES = SUPPLEMENTARY_PART_TYPES - {"film"}


@dataclass(frozen=True)
class LayoutAttachment:
    kind: str
    season_number: int | None = None


@dataclass(frozen=True)
class LayoutTitle:
    id: int
    collection_id: int | None
    hierarchy_type: str
    attachment: LayoutAttachment
    structural_valid: bool
    content_types: tuple[str, ...]
    interpretation: str
    own_metadata_identity: tuple[str, str] | None
    logical_count: int | None
    # Release text (IV markers, folder label) only blocks shared defaults; a
    # canonical rename removes it, so it is never part of the basis identity.
    interview_evidence: bool = False


@dataclass(frozen=True)
class LayoutChoice:
    id: int | None
    layout_kind: str
    confirmed_at: datetime
    basis_snapshot_json: str


@dataclass(frozen=True)
class PhysicalLayoutContext:
    titles: Mapping[int, LayoutTitle]
    choices: Mapping[int, LayoutChoice]


@dataclass(frozen=True)
class PhysicalLayoutResolution:
    owner_title: LayoutTitle | None
    choice: LayoutChoice | None
    effective_layout_kind: str | None
    authority: str
    basis_matches: bool | None
    diagnostics: tuple[str, ...]
    attachment: LayoutAttachment | None
    own_metadata_identity: tuple[str, str] | None
    logical_count: int | None
    review_class: str | None
    recommendation: str | None
    requires_human_decision: bool


def _count_bucket(count: int | None) -> str:
    if count is None:
        return "unknown"
    return "empty" if count == 0 else "singleton" if count == 1 else "multiple"


def create_basis_snapshot(context: PhysicalLayoutContext, title_id: int) -> str:
    title = context.titles.get(title_id)
    if title is None:
        raise ValueError("Unknown physical layout owner.")
    payload = {
        "version": 1,
        "owner_title_id": title.id,
        "collection_id": title.collection_id,
        "attachment": {"kind": title.attachment.kind, "season_number": title.attachment.season_number},
        "grouping_profile": {
            "container_type": title.hierarchy_type,
            "content_types": title.content_types,
            "interpretation": title.interpretation,
        },
        "own_metadata_identity": title.own_metadata_identity,
        # Multiplicity affects own-work recommendations. Variants, duplicates
        # and Media Parts never change this bucket just by adding physical rows.
        "logical_count_state": _count_bucket(title.logical_count),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _basis_matches(choice: LayoutChoice, current: str) -> bool:
    try:
        # Canonical JSON equality also distinguishes bool from integer identity.
        stored = json.dumps(json.loads(choice.basis_snapshot_json), ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    return stored == current


def _recommendation(title: LayoutTitle) -> tuple[str, str | None]:
    types = set(title.content_types)
    if (title.attachment.kind == "season" and types
        and ((title.interpretation == "story_preview" and not title.interview_evidence
              and types == {"preview"})
             or types == {"recap"})):
        return "DIRECT_SEASON", "direct_season"
    if title.own_metadata_identity is not None:
        if title.logical_count is None or title.logical_count == 0:
            return "NEEDS_HUMAN_LAYOUT", None
        return ("OWN_FOLDER_STRONG" if title.logical_count >= 2 else "OWN_FOLDER_OPTIONAL"), "own_folder"
    if title.interview_evidence:
        return "NEEDS_HUMAN_LAYOUT", None
    defaults = {
        "ova": "shared_ova", "special": "shared_specials",
        "op": "extras_openings_endings", "ed": "extras_openings_endings",
        "ncop": "extras_openings_endings", "nced": "extras_openings_endings",
        "cm": "extras_promo", "bonus": "extras_bonus", "menu": "extras_menus",
    }
    if title.hierarchy_type != "preview":
        defaults["preview"] = "extras_promo"
    groups = {defaults.get(kind) for kind in types}
    if groups and None not in groups and len(groups) == 1:
        return "SHARED_LAYOUT", groups.pop()
    return "NEEDS_HUMAN_LAYOUT", None


def resolve_physical_layout(context: PhysicalLayoutContext, title_id: int) -> PhysicalLayoutResolution:
    title = context.titles.get(title_id)
    choice = context.choices.get(title_id)
    if title is None:
        return PhysicalLayoutResolution(None, choice, None, "unresolved", None,
            ("owner_unavailable",), None, None, None, None, None, True)
    diagnostics = []
    eligible = title.hierarchy_type in LAYOUT_TITLE_TYPES
    if not eligible:
        diagnostics.append("not_supplementary_layout_owner")
    if not title.structural_valid:
        diagnostics.append("invalid_structural_attachment")
    review_class, recommendation = _recommendation(title)
    if title.logical_count is None:
        diagnostics.append("logical_count_unresolved")
    matches = _basis_matches(choice, create_basis_snapshot(context, title_id)) if choice else None
    effective = None
    authority = "unresolved"
    if choice:
        authority = "human_choice"
        if choice.layout_kind not in PHYSICAL_LAYOUT_KINDS:
            diagnostics.append("invalid_layout_kind")
        if not matches:
            diagnostics.append("basis_mismatch")
        if eligible and title.structural_valid and matches and choice.layout_kind in PHYSICAL_LAYOUT_KINDS:
            if choice.layout_kind == "direct_season" and title.attachment.kind != "season":
                diagnostics.append("direct_season_requires_season")
            else:
                effective = choice.layout_kind
    elif eligible and title.structural_valid:
        # A known shared taxonomy is independent of unresolved ordinals/counts.
        # Count uncertainty must not become a second Hierarchy Review in Layout.
        if review_class in {"DIRECT_SEASON", "SHARED_LAYOUT"}:
            effective, authority = recommendation, "derived_default"
        else:
            diagnostics.append("human_layout_required")
    return PhysicalLayoutResolution(
        title, choice, effective, authority, matches, tuple(diagnostics), title.attachment,
        title.own_metadata_identity, title.logical_count, review_class if eligible else None,
        recommendation if eligible else None, (eligible or choice is not None) and effective is None,
    )
