from __future__ import annotations

from .hierarchy import HierarchyIdentity
from .hierarchy_authority import manual_hierarchy_snapshot_requires_preservation
from .models import CatalogTitle, Video


def preserved_manual_assignment_title(
    video: Video,
    identity: HierarchyIdentity,
    titles_by_path: dict[str, CatalogTitle],
) -> CatalogTitle | None:
    """Resolve manual title-placement authority before automatic path inference."""
    current = video.catalog_title
    if (
        current is not None
        and manual_hierarchy_snapshot_requires_preservation(current)
        and current.collection is not None
    ):
        return current
    path_title = titles_by_path.get(identity.title.relative_root_path)
    if (
        path_title is not None
        and manual_hierarchy_snapshot_requires_preservation(path_title)
        and path_title.collection is not None
        and path_title.collection.relative_root_path
        != identity.collection.relative_root_path
    ):
        return path_title
    return None
