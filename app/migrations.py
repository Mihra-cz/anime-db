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
from .hierarchy_authority import manual_hierarchy_snapshot_requires_preservation
from .hierarchy_evaluation import finalize_collection_hierarchy
from .hierarchy_review import extract_local_period_hint
from .manual_split import (
    apply_manual_split_decisions,
    evaluate_persisted_manual_split,
    historical_manual_split_ambiguities,
    manual_split_titles,
    persisted_manual_split_authority_collections,
)
from .models import (
    CatalogCollection, CatalogTitle, ExternalSubtitle, ExternalTitleLink,
    InternalSubtitle, TitleMetadata, Video,
)

logger = logging.getLogger(__name__)


def migrate_schema(engine) -> None:
    """Apply the small, idempotent SQLite schema migration needed by v0.2."""
    inspector = inspect(engine)
    if "videos" not in inspector.get_table_names():
        return
    # Vytvoří pouze nové tabulky; existující tabulky ani data nemění.
    Base.metadata.create_all(engine)
    additions = {
        "audio_tracks": [("manual_language", "VARCHAR NULL")],
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
            ("media_part_number", "INTEGER NULL"),
            ("duplicate_status_manual", "VARCHAR NULL"),
            ("duplicate_of_video_id", "INTEGER NULL REFERENCES videos(id) ON DELETE SET NULL"),
            ("duplicate_primary_missing", "BOOLEAN NOT NULL DEFAULT 0"),
        ],
        "internal_subtitles": [("normalized_language", "VARCHAR NOT NULL DEFAULT 'unknown'")],
        "external_subtitles": [
            ("normalized_language", "VARCHAR NOT NULL DEFAULT 'unknown'"),
            ("manual_language", "VARCHAR NULL"),
        ],
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
            ("part_number_manual", "INTEGER NULL"),
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
            "CREATE INDEX IF NOT EXISTS ix_videos_duplicate_of_video_id "
            "ON videos(duplicate_of_video_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_videos_duplicate_status_manual "
            "ON videos(duplicate_status_manual)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_catalog_collections_hierarchy_status "
            "ON catalog_collections(hierarchy_status)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_manual_split_rule_videos_video_id "
            "ON manual_split_rule_videos(video_id)"
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
        created_automatic_titles: set[CatalogTitle] = set()
        created_automatic_collections: set[CatalogCollection] = set()
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
            title
            for title in original_titles
            if manual_hierarchy_snapshot_requires_preservation(title)
        )
        for identity in identities_by_title_path.values():
            if identity.title.relative_root_path == ROOT_FOLDER:
                continue
            part = identity.title
            title = titles.get(part.relative_root_path)
            if (
                title is not None
                and manual_hierarchy_snapshot_requires_preservation(title)
                and title.catalog_collection_id is not None
            ):
                used_titles.add(title)
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
                created_automatic_collections.add(collection)
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
                created_automatic_titles.add(title)
            title.catalog_collection_id = collection.id
            title.local_title = part.local_title
            title.normalized_local_title = normalize_title(part.local_title)
            if not manual_hierarchy_snapshot_requires_preservation(title):
                title.part_type = part.part_type
                title.season_number = part.season_number
                title.part_number = part.part_number
                title.season_label = part.season_label
                title.original_folder_name = part.original_folder_name
                title.sort_order = part.sort_order
            used_titles.add(title)

        videos_by_collection: dict[int, list[Video]] = {}
        protected_collection_ids: set[int] = set()
        for video in videos:
            authority_collections = persisted_manual_split_authority_collections(video)
            if video.manual_split_rule_videos:
                authority_is_valid = (
                    len(authority_collections) == 1
                    and all(
                        link.catalog_title is not None
                        and link.catalog_title.collection is authority_collections[0]
                        for link in video.manual_split_rule_videos
                    )
                )
                if authority_is_valid:
                    collection = authority_collections[0]
                    video.catalog_collection_id = collection.id
                    videos_by_collection.setdefault(collection.id, []).append(video)
                else:
                    protected_collection_ids.update(
                        collection.id
                        for collection in authority_collections
                        if collection.id is not None
                    )
                    if video.catalog_collection_id is not None:
                        protected_collection_ids.add(video.catalog_collection_id)
                    logger.warning(
                        "Video %s má nekonzistentní persistentní manual-split authority; "
                        "startup sync jeho hierarchy assignment nemění.",
                        video.relative_path,
                    )
                continue
            if is_root_video(video):
                assigned_collection = meaningful_root_collection(video) or video.catalog_collection
                if assigned_collection is not None:
                    videos_by_collection.setdefault(assigned_collection.id, []).append(video)
                continue
            identity = hierarchy[video.relative_path]
            automatic_title = titles[identity.title.relative_root_path]
            legacy_conflict_collection = (
                video.catalog_collection
                if video.catalog_title is None
                and video.catalog_collection is not None
                and video.catalog_collection.hierarchy_status == "conflict"
                and manual_split_titles(video.catalog_collection)
                else None
            )
            if legacy_conflict_collection is not None:
                video.catalog_collection_id = legacy_conflict_collection.id
                videos_by_collection.setdefault(
                    legacy_conflict_collection.id, []
                ).append(video)
                continue
            title = (
                video.catalog_title
                if video.catalog_title is not None
                and manual_hierarchy_snapshot_requires_preservation(
                    video.catalog_title
                )
                and video.catalog_title.collection is not None
                else automatic_title
            )
            collection = (
                title.collection
                if manual_hierarchy_snapshot_requires_preservation(title)
                and title.collection is not None
                else collections[identity.collection.relative_root_path]
            )
            video.catalog_collection_id = collection.id
            videos_by_collection.setdefault(collection.id, []).append(video)
            # Aktivní manual split se musí nejprve vyhodnotit jako celek.
            # Existující assignment zůstává na videu zachován, ale unassigned
            # video se nesmí před vyhodnocením připojit k prvnímu path title.
            if not manual_split_titles(collection):
                video.catalog_title_id = title.id

        for collection in collections.values():
            collection_videos = videos_by_collection.get(collection.id, [])
            if not collection_videos:
                continue
            collection.local_period_hint = extract_local_period_hint(collection.local_title)
            if manual_split_titles(collection):
                manual_split = evaluate_persisted_manual_split(
                    collection,
                    collection_videos,
                )
                if historical_manual_split_ambiguities(collection, manual_split):
                    protected_collection_ids.add(collection.id)
                    continue
                apply_manual_split_decisions(manual_split, collection)
                session.flush()
                assigned_title_ids = {
                    video.catalog_title_id for video in collection_videos
                    if video.catalog_title_id is not None
                }
                for title in list(titles.values()):
                    if (
                        title.catalog_collection_id != collection.id
                        or title.hierarchy_manual_override
                        or title.id in assigned_title_ids
                        or title.metadata_record is not None
                        or title.external_links
                        or title.metadata_candidates
                        or title.artwork
                        or title.manual_display_title
                        or title.preferred_metadata_provider
                        or title.preferred_external_id
                        or title.metadata_locked
                        or title.metadata_status != "unlinked"
                        or title.numbering_manual
                        or title.numbering_verified_at is not None
                        or title.season_number_manual is not None
                        or title.part_number_manual is not None
                        or title.season_label_manual
                        or title.part_type_manual
                        or title.sort_order_manual is not None
                        or title.episode_start is not None
                        or title.episode_end is not None
                        or title.episode_start_offset is not None
                        or title.episode_filename_pattern
                    ):
                        continue
                    # Startup nejprve odvodí title z fyzické cesty. Pokud však
                    # autoritativní manual split všechna videa přiřadil jinam,
                    # tento automatický prázdný mezivýsledek nesmí přežít sync.
                    session.delete(title)
                    titles.pop(title.relative_root_path, None)
                session.flush()
                continue

        # Identity derivation precedes global manual-split evaluation.  When
        # explicit authority redirects every video to another collection, the
        # just-created physical-path objects are disposable intermediates, not
        # persistent hierarchy.  Remove only objects created by this startup
        # run; pre-existing/user-bearing rows remain outside this cleanup.
        session.flush()
        assigned_title_ids = {
            title_id
            for title_id in session.scalars(
                select(Video.catalog_title_id).where(
                    Video.catalog_title_id.is_not(None)
                )
            )
            if title_id is not None
        }
        for title in created_automatic_titles:
            if (
                title.relative_root_path not in titles
                or title.id in assigned_title_ids
                or title.manual_split_rule_videos
            ):
                continue
            session.delete(title)
            titles.pop(title.relative_root_path, None)
        session.flush()
        for collection in created_automatic_collections:
            has_title = session.scalar(select(CatalogTitle.id).where(
                CatalogTitle.catalog_collection_id == collection.id
            )) is not None
            has_video = session.scalar(select(Video.id).where(
                Video.catalog_collection_id == collection.id
            )) is not None
            if has_title or has_video:
                continue
            session.delete(collection)
            collections.pop(collection.relative_root_path, None)
        session.flush()

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
        videos_by_collection_id: dict[int, list[Video]] = {}
        for video in session.scalars(select(Video)).all():
            if video.catalog_collection_id:
                videos_by_collection_id.setdefault(
                    video.catalog_collection_id, []
                ).append(video)
        for collection in session.scalars(select(CatalogCollection).options(
            selectinload(CatalogCollection.titles).selectinload(CatalogTitle.metadata_record)
        )).all():
            collection_videos = videos_by_collection_id.get(collection.id, [])
            if not collection_videos or collection.id in protected_collection_ids:
                continue
            finalize_collection_hierarchy(
                collection,
                collection_videos,
            )
        session.commit()
