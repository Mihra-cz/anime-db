from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .hierarchy import derive_library_hierarchy
from .hierarchy_authority import manual_hierarchy_snapshot_requires_preservation
from .models import CatalogCollection, CatalogTitle, Video


RELATED_NAMED_CHILD_REVIEW_REASON = (
    "Část s vlastním názvem byla seskupena podle společného fyzického parentu a "
    "příbuzného názvu; vztah vyžaduje ruční potvrzení."
)
SUPPLEMENTARY_NAMED_CHILD_REVIEW_REASON = (
    "Doplňková část zachovává kontext vlastního child názvu, ale související "
    "season vyžaduje ruční potvrzení."
)


class NamedChildProvenanceKind(StrEnum):
    RELATED_NAMED_CHILD = "related_named_child"
    SUPPLEMENTARY_NAMED_CHILD = "supplementary_named_child"


@dataclass(frozen=True)
class CatalogTitlePathProvenance:
    """Deterministic path context for one currently automatic CatalogTitle."""

    kind: NamedChildProvenanceKind
    catalog_title: CatalogTitle
    videos: tuple[Video, ...]


def derive_collection_path_provenance(
    collection: CatalogCollection,
    videos: list[Video] | None = None,
) -> tuple[CatalogTitlePathProvenance, ...]:
    """Re-derive named-child context from stored paths using the assignment parser.

    Provenance is accepted only when the parser-derived collection and title paths
    still match the current automatic assignment. A manual hierarchy assignment is
    authoritative and deliberately suppresses the automatic path warning.
    """
    all_videos = list(collection.videos if videos is None else videos)
    if not all_videos:
        return ()
    derived = derive_library_hierarchy([
        video.relative_path for video in all_videos
    ])
    titles_by_id = {
        title.id: title for title in collection.titles if title.id is not None
    }
    grouped: dict[
        tuple[NamedChildProvenanceKind, CatalogTitle], list[Video]
    ] = {}
    for video in all_videos:
        identity = derived.get(video.relative_path)
        if identity is None:
            continue
        try:
            kind = NamedChildProvenanceKind(identity.title.detection_reason)
        except ValueError:
            continue
        title = video.catalog_title
        if title is None and video.catalog_title_id is not None:
            title = titles_by_id.get(video.catalog_title_id)
        if (
            title is None
            or manual_hierarchy_snapshot_requires_preservation(title)
            or identity.collection.relative_root_path
            != collection.relative_root_path
            or identity.title.relative_root_path != title.relative_root_path
        ):
            continue
        grouped.setdefault((kind, title), []).append(video)
    return tuple(
        CatalogTitlePathProvenance(
            kind=kind,
            catalog_title=title,
            videos=tuple(sorted(items, key=lambda video: video.relative_path)),
        )
        for (kind, title), items in sorted(
            grouped.items(),
            key=lambda item: (
                item[0][0].value,
                item[0][1].relative_root_path,
            ),
        )
    )
