from __future__ import annotations

import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, selectinload

from .catalog import (
    ROOT_FOLDER, classify_video, is_root_video, meaningful_root_collection,
    normalize_language, normalize_title,
)
from .database import Base
from .hierarchy import derive_library_hierarchy
from .hierarchy_review import (
    collection_requires_review, definition_from_title, extract_local_period_hint,
    manual_split_titles, preview_assignments,
)
from .models import (
    CatalogCollection, CatalogTitle, ExternalSubtitle, ExternalTitleLink,
    InternalSubtitle, TitleMetadata, Video, utc_now,
)
from .numbering import recalculate_collection_numbering

logger = logging.getLogger(__name__)


def migrate_schema(engine) -> None:
    """Apply the small, idempotent SQLite schema migration needed by v0.2."""
    inspector = inspect(engine)
    if "videos" not in inspector.get_table_names():
        return
    # Vytvoří pouze nové tabulky; existující tabulky ani data nemění.
    Base.metadata.create_all(engine)
    additions = {
        "videos": [
            ("file_type", "VARCHAR NOT NULL DEFAULT 'other'"),
            ("manual_hardsub_cs", "BOOLEAN NOT NULL DEFAULT 0"),
            ("manual_hardsub_sk", "BOOLEAN NOT NULL DEFAULT 0"),
            ("manual_hardsub_verified_at", "DATETIME NULL"),
            ("catalog_title_id", "INTEGER NULL REFERENCES catalog_titles(id)"),
            ("catalog_collection_id", "INTEGER NULL REFERENCES catalog_collections(id)"),
            ("local_episode_number", "INTEGER NULL"),
            ("season_episode_number", "INTEGER NULL"),
            ("absolute_episode_number", "INTEGER NULL"),
            ("external_episode_number", "INTEGER NULL"),
            ("episode_number_source", "VARCHAR NOT NULL DEFAULT 'unknown'"),
            ("episode_number_confidence", "FLOAT NULL"),
            ("episode_number_manual_override", "INTEGER NULL"),
            ("episode_number_verified_at", "DATETIME NULL"),
            ("content_type_manual", "VARCHAR NULL"),
        ],
        "internal_subtitles": [("normalized_language", "VARCHAR NOT NULL DEFAULT 'unknown'")],
        "external_subtitles": [("normalized_language", "VARCHAR NOT NULL DEFAULT 'unknown'")],
        "title_metadata": [("cover_image_url", "VARCHAR NULL")],
        "catalog_titles": [
            ("catalog_collection_id", "INTEGER NULL REFERENCES catalog_collections(id)"),
            ("part_type", "VARCHAR NOT NULL DEFAULT 'title'"),
            ("season_number", "INTEGER NULL"),
            ("season_label", "VARCHAR NULL"),
            ("original_folder_name", "VARCHAR NULL"),
            ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
            ("part_number", "INTEGER NULL"),
            ("episode_start_offset", "INTEGER NULL"),
            ("numbering_mode", "VARCHAR NOT NULL DEFAULT 'unknown'"),
            ("numbering_manual", "BOOLEAN NOT NULL DEFAULT 0"),
            ("numbering_verified_at", "DATETIME NULL"),
            ("hierarchy_manual_override", "BOOLEAN NOT NULL DEFAULT 0"),
            ("season_number_manual", "INTEGER NULL"),
            ("season_label_manual", "VARCHAR NULL"),
            ("part_type_manual", "VARCHAR NULL"),
            ("sort_order_manual", "INTEGER NULL"),
            ("hierarchy_verified_at", "DATETIME NULL"),
            ("episode_start", "INTEGER NULL"),
            ("episode_end", "INTEGER NULL"),
            ("episode_filename_pattern", "VARCHAR NULL"),
        ],
        "catalog_collections": [
            ("hierarchy_status", "VARCHAR NOT NULL DEFAULT 'automatic'"),
            ("hierarchy_verified_at", "DATETIME NULL"),
            ("hierarchy_note", "TEXT NULL"),
            ("local_period_hint", "VARCHAR NULL"),
        ],
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            if table not in inspect(connection).get_table_names():
                continue
            existing = {column["name"] for column in inspect(connection).get_columns(table)}
            for name, definition in columns:
                if name not in existing:
                    logger.info("Migrace databáze: přidávám %s.%s", table, name)
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_external_title_primary "
            "ON external_title_links(catalog_title_id) WHERE is_primary = 1"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_catalog_titles_catalog_collection_id "
            "ON catalog_titles(catalog_collection_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_videos_catalog_collection_id "
            "ON videos(catalog_collection_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_catalog_collections_hierarchy_status "
            "ON catalog_collections(hierarchy_status)"
        ))

    with Session(engine) as session:
        for video in session.scalars(select(Video)):
            video.file_type = classify_video(video.relative_path)
        for subtitle in session.scalars(select(InternalSubtitle)):
            subtitle.normalized_language = normalize_language(subtitle.language, subtitle.title)
        for subtitle in session.scalars(select(ExternalSubtitle)):
            subtitle.normalized_language = normalize_language(subtitle.language)
        videos = list(session.scalars(select(Video).order_by(Video.id)))
        hierarchy = derive_library_hierarchy([video.relative_path for video in videos])
        titles = {
            title.relative_root_path: title
            for title in session.scalars(select(CatalogTitle)).all()
        }
        original_titles = set(titles.values())
        collections = {
            collection.relative_root_path: collection
            for collection in session.scalars(select(CatalogCollection)).all()
        }
        identities_by_title_path = {
            identity.title.relative_root_path: identity
            for identity in hierarchy.values()
        }
        used_titles: set[CatalogTitle] = {
            video.catalog_title
            for video in videos
            if is_root_video(video)
            and video.catalog_title is not None
        }
        used_titles.update(
            title for title in original_titles if title.hierarchy_manual_override
        )
        for identity in identities_by_title_path.values():
            if identity.title.relative_root_path == ROOT_FOLDER:
                continue
            collection_identity = identity.collection
            collection = collections.get(collection_identity.relative_root_path)
            if collection is None:
                collection = CatalogCollection(
                    local_title=collection_identity.local_title,
                    normalized_local_title=normalize_title(collection_identity.local_title),
                    relative_root_path=collection_identity.relative_root_path,
                )
                session.add(collection)
                session.flush()
                collections[collection.relative_root_path] = collection
            part = identity.title
            title = titles.get(part.relative_root_path)
            if title is None:
                title = CatalogTitle(
                    local_title=part.local_title,
                    normalized_local_title=normalize_title(part.local_title),
                    relative_root_path=part.relative_root_path,
                )
                session.add(title)
                session.flush()
                titles[part.relative_root_path] = title
            title.catalog_collection_id = collection.id
            title.local_title = part.local_title
            title.normalized_local_title = normalize_title(part.local_title)
            if not title.hierarchy_manual_override:
                title.part_type = part.part_type
                title.season_number = part.season_number
                title.season_label = part.season_label
                title.original_folder_name = part.original_folder_name
                title.sort_order = part.sort_order
                title.part_number = (
                    part.season_number if part.part_type in {"part", "cour"} else None
                )
            used_titles.add(title)

        videos_by_collection: dict[int, list[Video]] = {}
        for video in videos:
            if is_root_video(video):
                assigned_collection = meaningful_root_collection(video) or video.catalog_collection
                if assigned_collection is not None:
                    videos_by_collection.setdefault(assigned_collection.id, []).append(video)
                continue
            identity = hierarchy[video.relative_path]
            title = titles[identity.title.relative_root_path]
            collection = collections[identity.collection.relative_root_path]
            video.catalog_collection_id = collection.id
            videos_by_collection.setdefault(collection.id, []).append(video)
            if not manual_split_titles(collection):
                video.catalog_title_id = title.id

        for collection in collections.values():
            collection.local_period_hint = extract_local_period_hint(collection.local_title)
            collection_videos = videos_by_collection.get(collection.id, [])
            split_titles = sorted(
                manual_split_titles(collection), key=lambda title: title.effective_sort_order
            )
            if split_titles:
                preview = preview_assignments(
                    collection_videos, [definition_from_title(title) for title in split_titles]
                )
                for video in collection_videos:
                    target = preview.assignments.get(video.id)
                    if target is not None:
                        video.catalog_title = split_titles[target]
                    elif (
                        video.catalog_title is None
                        or video.catalog_title.catalog_collection_id != collection.id
                    ):
                        video.catalog_title = None
                unresolved_ids = tuple(
                    video.id for video in collection_videos
                    if video.id in preview.unmatched_video_ids and video.catalog_title is None
                )
                if preview.conflicts:
                    collection.hierarchy_status = "conflict"
                    collection.hierarchy_note = "Video odpovídá více ručním částem."
                    collection.hierarchy_verified_at = None
                elif unresolved_ids:
                    collection.hierarchy_status = "review_required"
                    collection.hierarchy_note = "Nové nezařazené video."
                    collection.hierarchy_verified_at = None
                else:
                    collection.hierarchy_status = "verified"
                    collection.hierarchy_note = None
                    collection.hierarchy_verified_at = (
                        collection.hierarchy_verified_at or utc_now()
                    )
                continue
            reason = collection_requires_review(collection, collection_videos)
            if collection.hierarchy_status != "verified":
                collection.hierarchy_status = "review_required" if reason else "automatic"
                collection.hierarchy_note = reason

        check_sql = " ".join(
            constraint.get("sqltext") or ""
            for constraint in inspect(engine).get_check_constraints("catalog_titles")
        )
        review_status = (
            "migration_review_required"
            if not check_sql or "migration_review_required" in check_sql
            else "conflict"
        )
        for legacy in original_titles - used_titles:
            has_metadata = session.get(TitleMetadata, legacy.id) is not None
            has_links = session.scalar(select(ExternalTitleLink.id).where(
                ExternalTitleLink.catalog_title_id == legacy.id
            )) is not None
            if not has_metadata and not has_links:
                session.delete(legacy)
                continue
            matching_collection = next(
                (
                    collection for path, collection in collections.items()
                    if legacy.relative_root_path == path
                    or legacy.relative_root_path.startswith(f"{path}/")
                ),
                None,
            )
            if matching_collection:
                legacy.catalog_collection_id = matching_collection.id
            legacy.part_type = "migration_review"
            legacy.season_number = None
            legacy.season_label = None
            legacy.sort_order = -1
            legacy.metadata_status = review_status
        session.flush()
        session.expire_all()
        videos_by_title: dict[int, list[Video]] = {}
        for video in session.scalars(select(Video)).all():
            if video.catalog_title_id:
                videos_by_title.setdefault(video.catalog_title_id, []).append(video)
        for collection in session.scalars(select(CatalogCollection).options(
            selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_record)
        )).all():
            recalculate_collection_numbering(collection, videos_by_title)
        session.commit()
