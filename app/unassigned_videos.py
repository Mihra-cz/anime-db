from __future__ import annotations

from dataclasses import dataclass

from .catalog import ROOT_FOLDER
from .models import Video


@dataclass(frozen=True)
class InsufficientVideoAssignment:
    """One known video without a complete, safe logical hierarchy chain."""

    video: Video
    code: str
    reason: str


@dataclass(frozen=True)
class InsufficientVideoAssignmentSummary:
    """Projected overview item that does not require ORM relationship loads."""

    video_id: int
    filename: str
    relative_path: str
    code: str
    reason: str


_ASSIGNMENT_REASONS = {
    "missing_catalog_title": "Video není přiřazeno ke konkrétní části (CatalogTitle).",
    "missing_title_collection": (
        "Přiřazená část nepatří k žádnému anime (CatalogCollection)."
    ),
    "technical_root_placeholder": (
        "Video má pouze technické původní zařazení ke kořeni knihovny."
    ),
    "missing_video_collection": (
        "Video nemá úplnou vazbu na anime své přiřazené části."
    ),
    "collection_mismatch": "Vazba videa na anime neodpovídá přiřazené části.",
}


def insufficient_video_assignment_kind(
    *, title_exists: bool, title_collection_exists: bool,
    title_collection_id: int | None,
    title_collection_path: str | None, video_collection_id: int | None,
) -> tuple[str, str] | None:
    """Evaluate projected IDs with the same invariant as the ORM helper."""
    code = None
    if not title_exists:
        code = "missing_catalog_title"
    elif not title_collection_exists:
        code = "missing_title_collection"
    elif title_collection_path == ROOT_FOLDER:
        code = "technical_root_placeholder"
    elif video_collection_id is None:
        code = "missing_video_collection"
    elif video_collection_id != title_collection_id:
        code = "collection_mismatch"
    return (code, _ASSIGNMENT_REASONS[code]) if code is not None else None


def insufficient_video_assignment(
    video: Video,
) -> InsufficientVideoAssignment | None:
    """Return the current assignment blocker independently of physical depth.

    A usable logical assignment always resolves through one concrete
    CatalogTitle to its CatalogCollection, and the redundant Video collection
    link must agree with that chain.  The legacy ``.`` collection is only a
    technical placeholder for the physical library root, not an anime.
    """
    title = video.catalog_title
    collection = title.collection if title is not None else None
    if (
        collection is not None
        and collection.relative_root_path != ROOT_FOLDER
        and video.catalog_collection is collection
    ):
        return None
    kind = insufficient_video_assignment_kind(
        title_exists=title is not None,
        title_collection_exists=collection is not None,
        title_collection_id=collection.id if collection is not None else None,
        title_collection_path=(
            collection.relative_root_path if collection is not None else None
        ),
        video_collection_id=(
            video.catalog_collection.id
            if video.catalog_collection is not None
            else video.catalog_collection_id
        ),
    )
    if kind is None:
        return None
    code, reason = kind
    return InsufficientVideoAssignment(video=video, code=code, reason=reason)


def insufficient_video_assignments(
    videos: list[Video] | tuple[Video, ...],
) -> tuple[InsufficientVideoAssignment, ...]:
    return tuple(
        assignment
        for video in videos
        if (assignment := insufficient_video_assignment(video)) is not None
    )
