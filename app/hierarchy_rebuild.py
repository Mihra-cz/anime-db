from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import normalize_title
from .hierarchy import derive_library_hierarchy
from .models import CatalogCollection, CatalogTitle, Video


@dataclass(frozen=True)
class HierarchyChange:
    title_id: int
    original_folder_name: str
    normalized_base: str
    old_season_label: str | None
    new_season_label: str | None
    collection_path: str
    reason: str


def rebuild_hierarchy(session: Session, *, apply: bool = False) -> list[HierarchyChange]:
    videos = list(session.scalars(select(Video).order_by(Video.relative_path)))
    hierarchy = derive_library_hierarchy([video.relative_path for video in videos])
    titles = {
        title.relative_root_path: title
        for title in session.scalars(select(CatalogTitle)).all()
    }
    collections = {
        collection.relative_root_path: collection
        for collection in session.scalars(select(CatalogCollection)).all()
    }
    changes: list[HierarchyChange] = []
    seen_titles: set[int] = set()
    for identity in hierarchy.values():
        title = titles.get(identity.title.relative_root_path)
        if title is None or title.id in seen_titles or title.hierarchy_manual_override:
            continue
        seen_titles.add(title.id)
        target = identity.title
        collection_changed = (
            title.collection is None
            or title.collection.relative_root_path != identity.collection.relative_root_path
        )
        if not collection_changed and (
            title.season_number, title.season_label, title.part_type
        ) == (target.season_number, target.season_label, target.part_type):
            continue
        changes.append(HierarchyChange(
            title.id, target.original_folder_name or title.local_title,
            target.normalized_base, title.season_label, target.season_label,
            identity.collection.relative_root_path, target.detection_reason,
        ))
        if apply:
            collection = collections.get(identity.collection.relative_root_path)
            if collection is None:
                collection = CatalogCollection(
                    local_title=identity.collection.local_title,
                    normalized_local_title=normalize_title(identity.collection.local_title),
                    relative_root_path=identity.collection.relative_root_path,
                )
                session.add(collection)
                session.flush()
                collections[collection.relative_root_path] = collection
            title.collection = collection
            title.season_number = target.season_number
            title.season_label = target.season_label
            title.part_type = target.part_type
            title.original_folder_name = target.original_folder_name
            title.sort_order = target.sort_order
            title.part_number = (
                target.season_number if target.part_type in {"part", "cour"} else None
            )
    if apply:
        session.commit()
    else:
        session.rollback()
    return changes
