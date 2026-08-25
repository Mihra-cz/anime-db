from __future__ import annotations

from .hierarchy_authority import manual_hierarchy_snapshot_is_complete
from .hierarchy_types import PART_TYPE_CHOICES
from .models import CatalogTitle


_PART_TYPE_RANK = {
    value: rank for rank, (value, _) in enumerate(PART_TYPE_CHOICES)
}
_PART_TYPE_RANK["cour"] = _PART_TYPE_RANK["part"]


def catalog_title_structural_sort_key(
    title: CatalogTitle,
) -> tuple[bool, int, int, bool, int, str, int]:
    """Stable automatic order derived only from persisted hierarchy structure."""
    season_number = title.effective_season_number
    part_number = title.effective_part_number
    part_type = title.effective_part_type
    return (
        season_number is None,
        season_number or 0,
        _PART_TYPE_RANK.get(part_type, len(_PART_TYPE_RANK)),
        part_number is None,
        part_number or 0,
        (title.local_title or "").casefold(),
        title.id or 0,
    )


def catalog_title_sort_key(
    title: CatalogTitle,
) -> tuple[int, int, bool, int, int, bool, int, str, int]:
    """Apply an explicit manual order before the central structural fallback.

    Historical non-NULL manual values remain authoritative because the current
    schema has no provenance capable of distinguishing a user value from an old
    workflow-generated value.  New automatic operations leave the field NULL.
    """
    structural = catalog_title_structural_sort_key(title)
    manual_order = (
        title.sort_order_manual
        if manual_hierarchy_snapshot_is_complete(title)
        else None
    )
    return (
        0 if manual_order is not None else 1,
        manual_order or 0,
        *structural,
    )
