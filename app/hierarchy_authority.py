from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from .hierarchy_types import PART_TYPES


class ManualHierarchySnapshot(Protocol):
    hierarchy_manual_override: bool
    hierarchy_verified_at: object | None
    part_type_manual: str | None
    season_number_manual: int | None
    part_number_manual: int | None


class MutableManualHierarchySnapshot(ManualHierarchySnapshot, Protocol):
    season_label_manual: str | None
    sort_order_manual: int | None


class ManualHierarchyAuthorityState(StrEnum):
    NONE = "none"
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


def structural_hierarchy_issue(
    part_type: str | None,
    season_number: int | None,
    part_number: int | None,
) -> str | None:
    """Validate one concrete authoritative structural identity."""
    if part_type not in PART_TYPES:
        return "Pro ruční zařazení zvolte konkrétní typ části."
    if season_number is not None and season_number <= 0:
        return "Číslo sezóny musí být kladné."
    if part_number is not None and part_number <= 0:
        return "Číslo Part musí být kladné."
    if part_type == "part" and part_number is None:
        return "Pro typ Part potvrďte číslo Part."
    if part_type == "season" and part_number is not None:
        return "Samostatná sezóna nesmí mít číslo Part."
    if part_type not in {"part", "cour"} and part_number is not None:
        return "Číslo Part lze uložit pouze pro typ Part."
    return None


def manual_hierarchy_snapshot_issue(
    title: ManualHierarchySnapshot,
) -> str | None:
    """Validate persisted manual authority without repairing historical data."""
    if not title.hierarchy_manual_override:
        return "Chybí autoritativní ruční hierarchy override."
    return structural_hierarchy_issue(
        title.part_type_manual,
        title.season_number_manual,
        title.part_number_manual,
    )


def manual_hierarchy_authority_state(
    title: ManualHierarchySnapshot,
) -> ManualHierarchyAuthorityState:
    """Classify manual hierarchy independently of assignment and selectors."""
    if not title.hierarchy_manual_override:
        return (
            ManualHierarchyAuthorityState.INCOMPLETE
            if title.hierarchy_verified_at is not None
            else ManualHierarchyAuthorityState.NONE
        )
    if manual_hierarchy_snapshot_issue(title) is None:
        return ManualHierarchyAuthorityState.COMPLETE
    return ManualHierarchyAuthorityState.INCOMPLETE


def manual_hierarchy_snapshot_is_complete(
    title: ManualHierarchySnapshot,
) -> bool:
    return (
        manual_hierarchy_authority_state(title)
        == ManualHierarchyAuthorityState.COMPLETE
    )


def manual_hierarchy_snapshot_requires_preservation(
    title: ManualHierarchySnapshot,
) -> bool:
    """Protect historical manual decisions without making them effective authority.

    A complete snapshot supplies effective structural values.  An incomplete
    historical snapshot does not, but automatic reconciliation must still keep
    its persisted title placement and automatic fields intact until a user
    completes or resets the decision.  Inactive manual values without either an
    override or a verification marker remain ordinary non-authoritative data.
    """
    return (
        manual_hierarchy_authority_state(title)
        != ManualHierarchyAuthorityState.NONE
    )


def manual_hierarchy_snapshot_uses_legacy_projection(
    title: ManualHierarchySnapshot,
) -> bool:
    """Keep 4B's active per-field fallback without granting complete authority.

    This compatibility projection is only for an already active historical
    override.  It keeps persisted 4B numbering semantics stable while the
    incomplete snapshot remains a blocking review issue.  Callers deciding
    verification or authority must use ``manual_hierarchy_authority_state``.
    """
    return (
        title.hierarchy_manual_override
        and manual_hierarchy_authority_state(title)
        == ManualHierarchyAuthorityState.INCOMPLETE
    )


def activate_manual_hierarchy_snapshot(
    title: MutableManualHierarchySnapshot,
    *,
    part_type: str,
    season_number: int | None,
    part_number: int | None,
    season_label: str | None,
    sort_order: int | None,
    verified_at: object,
) -> None:
    """Persist one already explicit, complete snapshot as a single mutation."""
    if issue := structural_hierarchy_issue(
        part_type,
        season_number,
        part_number,
    ):
        raise ValueError(issue)
    title.part_type_manual = part_type
    title.season_number_manual = season_number
    title.part_number_manual = part_number
    title.season_label_manual = season_label
    title.sort_order_manual = sort_order
    title.hierarchy_manual_override = True
    title.hierarchy_verified_at = verified_at


def clear_manual_hierarchy_snapshot(
    title: MutableManualHierarchySnapshot,
) -> None:
    """Clear hierarchy identity only; selector authority is deliberately separate."""
    title.season_number_manual = None
    title.part_number_manual = None
    title.season_label_manual = None
    title.part_type_manual = None
    title.sort_order_manual = None
    title.hierarchy_manual_override = False
    title.hierarchy_verified_at = None
