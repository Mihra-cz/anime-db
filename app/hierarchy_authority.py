from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
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


class EffectiveHierarchyTitle(Protocol):
    @property
    def effective_part_type(self) -> str: ...

    @property
    def effective_season_number(self) -> int | None: ...

    @property
    def effective_part_number(self) -> int | None: ...


@dataclass(frozen=True)
class SplitSeasonStructureIssue:
    season_number: int
    titles: tuple[EffectiveHierarchyTitle, ...]
    missing_part_number: bool
    duplicate_part_numbers: tuple[int, ...]

    @property
    def message(self) -> str:
        prefix = f"Season {self.season_number} už v této kolekci existuje."
        if self.missing_part_number:
            return (
                f"{prefix} Pokud jde o úmyslně rozdělenou sezónu, nastavte "
                "všem jejím částem unikátní explicitní číslo Part. "
                "Chybějící Part 1 se automaticky nedoplňuje."
            )
        duplicate_labels = ", ".join(
            f"Part {number}" for number in self.duplicate_part_numbers
        )
        return (
            f"{prefix} {duplicate_labels} je použito vícekrát; každá část "
            "rozdělené sezóny musí mít unikátní explicitní číslo Part."
        )


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
    if part_type not in {"season", "part", "cour"} and part_number is not None:
        return "Číslo Part lze uložit pouze pro Season nebo Part."
    return None


def split_season_structure_issues(
    titles: Iterable[EffectiveHierarchyTitle],
    *,
    override_title: EffectiveHierarchyTitle | None = None,
    override_part_type: str | None = None,
    override_season_number: int | None = None,
    override_part_number: int | None = None,
) -> tuple[SplitSeasonStructureIssue, ...]:
    """Find ambiguous sibling Season identities without mutating stored data."""
    by_season: dict[int, list[tuple[EffectiveHierarchyTitle, int | None]]] = {}
    for title in titles:
        if title is override_title:
            part_type = override_part_type
            season_number = override_season_number
            part_number = override_part_number
        else:
            part_type = title.effective_part_type
            season_number = title.effective_season_number
            part_number = title.effective_part_number
        if part_type == "season" and season_number is not None:
            by_season.setdefault(season_number, []).append((title, part_number))

    issues: list[SplitSeasonStructureIssue] = []
    for season_number, members in sorted(by_season.items()):
        if len(members) < 2:
            continue
        part_numbers = [part_number for _, part_number in members]
        duplicates = tuple(sorted({
            number for number in part_numbers
            if number is not None and part_numbers.count(number) > 1
        }))
        missing = any(number is None for number in part_numbers)
        if missing or duplicates:
            issues.append(SplitSeasonStructureIssue(
                season_number=season_number,
                titles=tuple(title for title, _ in members),
                missing_part_number=missing,
                duplicate_part_numbers=duplicates,
            ))
    return tuple(issues)


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
