from __future__ import annotations

from .catalog import normalize_title
from .hierarchy import derive_library_hierarchy, parse_explicit_part
from .hierarchy_types import PART_TYPE_LABELS
from .models import CatalogCollection, CatalogTitle, Video


def generic_catalog_title_local_title(
    part_type: str,
    *,
    season_number: int | None = None,
    season_label: str | None = None,
) -> str:
    """Return a conservative local identity without consulting a filename."""
    label = PART_TYPE_LABELS.get(
        part_type, part_type.replace("_", " ").title()
    ).strip() or "Část"
    context = (season_label or "").strip()
    if not context and season_number is not None:
        context = f"S{season_number}"
    return f"{label} – {context}"[:200] if context else label[:200]


def safe_catalog_title_local_title(
    *,
    explicit_local_title: str | None,
    part_type: str,
    collection: CatalogCollection | None = None,
    videos: list[Video] | tuple[Video, ...] = (),
    season_number: int | None = None,
    season_label: str | None = None,
    source_title: CatalogTitle | None = None,
) -> str:
    """Resolve manual text first, then shared structural context, never Video 1.

    Parser-derived context is accepted only when every selected video resolves
    to the same title identity inside the requested collection.  A source title
    can supply a meaningful series context when a supplementary group is split
    out of a main season.  Otherwise the type/season fallback is deterministic.
    """
    manual = (explicit_local_title or "").strip()[:200]
    if manual:
        return manual

    selected = tuple(videos)
    if selected:
        identities = derive_library_hierarchy([
            video.relative_path for video in selected
        ])
        candidates = {
            identity.title.local_title.strip()
            for identity in identities.values()
            if identity.title.local_title.strip()
            and identity.title.part_type == part_type
            and (
                collection is None
                or identity.collection.relative_root_path
                == collection.relative_root_path
            )
        }
        if len(candidates) == 1:
            candidate = candidates.pop()[:200]
            if (
                parse_explicit_part(candidate) is None
                and (
                    source_title is None
                    or normalize_title(candidate)
                    != normalize_title(source_title.local_title)
                )
            ):
                return candidate

    source = source_title
    if source is None:
        source_titles = {
            video.catalog_title for video in selected
            if video.catalog_title is not None
        }
        source = source_titles.pop() if len(source_titles) == 1 else None
    if (
        source is not None
        and source.effective_part_type in {"season", "title"}
        and (source.local_title or "").strip()
    ):
        type_label = PART_TYPE_LABELS.get(
            part_type, part_type.replace("_", " ").title()
        ).strip()
        return f"{type_label} – {source.local_title.strip()}"[:200]

    return generic_catalog_title_local_title(
        part_type,
        season_number=season_number,
        season_label=season_label,
    )
