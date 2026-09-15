from __future__ import annotations

from collections.abc import Mapping

from .hierarchy import HierarchyIdentity
from .hierarchy_authority import manual_hierarchy_snapshot_requires_preservation
from .models import CatalogCollection, CatalogTitle, Video


def preserved_membership_title(video: Video) -> CatalogTitle | None:
    """Keep membership that automatic inference could not reconstruct itself.

    A video already sitting in a manually verified title carries a grouping
    decision from before the explicit selector model, and nothing else records
    it.  Rebuilding that video from its physical path would silently discard the
    decision, so its own current placement is preserved.

    This protects the placement the video already has.  It deliberately says
    nothing about other videos: see :func:`automatic_assignment_title`.
    """
    current = video.catalog_title
    if (
        current is not None
        and current.collection is not None
        and manual_hierarchy_snapshot_requires_preservation(current)
    ):
        return current
    return None


def automatic_assignment_title(
    identity: HierarchyIdentity,
    titles_by_path: Mapping[str, CatalogTitle],
) -> CatalogTitle | None:
    """Resolve the one already existing title the video's own path points at.

    This is the only membership evidence automatic inference may create.  That a
    title carries a manual hierarchy snapshot is structural authority over that
    title and never proves that some video found under its path belongs to it,
    so no snapshot is consulted here.  ``None`` means automatic inference has no
    safely documented answer and the caller must leave the video for review
    rather than guess or invent a target.
    """
    return titles_by_path.get(identity.title.relative_root_path)


def structural_placement_collection(
    title: CatalogTitle,
    path_collection: CatalogCollection | None,
) -> CatalogCollection | None:
    """Resolve which collection THIS title itself belongs to.

    A persisted manual snapshot protects the title's own placement, which is
    what keeps a confirmed merge or move from being undone by automatic path
    reconstruction.  The answer describes the title, not the membership of any
    video, and callers must not read it as an assignment decision.
    """
    if (
        title.collection is not None
        and manual_hierarchy_snapshot_requires_preservation(title)
    ):
        return title.collection
    return path_collection
