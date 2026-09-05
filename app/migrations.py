from __future__ import annotations

import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, selectinload

from .catalog import (
    ROOT_FOLDER, classify_video, is_root_video, meaningful_root_collection,
    normalize_language, normalize_title,
)
from .database import Base
from .external_subtitle_compatibility import (
    backfill_legacy_external_subtitle_compatibilities,
    consolidate_legacy_external_subtitle_assets,
)
from .hierarchy import derive_library_hierarchy
from .hierarchy_authority import manual_hierarchy_snapshot_requires_preservation
from .hierarchy_evaluation import finalize_collection_hierarchy
from .hierarchy_review import (
    apply_collection_grouping_authority,
    collection_grouping_authority_targets,
    extract_local_period_hint,
)
from .manual_split import (
    apply_manual_split_decisions,
    evaluate_persisted_manual_split,
    historical_manual_split_ambiguities,
    manual_split_titles,
    persisted_manual_split_authority_collections,
)
from .models import (
    CatalogCollection, CatalogTitle, ExternalSubtitle, ExternalTitleLink,
    InternalSubtitle, TitleMetadata, Video, VideoVariantGroup,
)
from .structural_inference import infer_automatic_structural_values
from .video_variants import assign_video_catalog_title

logger = logging.getLogger(__name__)


# SQLite's native application-version marker separates one-time compatibility
# reconstruction from ordinary stable startup. Version 2 adds only a nullable
# workflow column to version 1; it does not require another library rebuild.
STARTUP_COMPATIBILITY_VERSION = 2


AutomaticStructuralInput = tuple[str, int | None, int | None, str | None]


def _legacy_external_subtitle_links(engine) -> tuple[tuple[int, int | None, str], ...]:
    """Read the old owner bridge without requiring it in the target ORM."""
    with engine.connect() as connection:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("external_subtitles")
        }
        if "video_id" not in columns:
            return ()
        return tuple(
            (int(row.id), row.video_id, str(row.match_method or "automatic"))
            for row in connection.execute(text(
                "SELECT id, video_id, match_method FROM external_subtitles "
                "ORDER BY id"
            ))
        )


def _retire_external_subtitle_video_id(engine) -> bool:
    """Atomically rebuild the SQLite asset table without the owner FK."""
    with engine.connect() as connection:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("external_subtitles")
        }
    if "video_id" not in columns:
        return False
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            "Odstranění ExternalSubtitle.video_id je implementováno pouze pro SQLite."
        )

    dbapi_connection = engine.raw_connection()
    try:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "CREATE TABLE external_subtitles_ownerless_migration ("
                "id INTEGER NOT NULL, "
                "relative_path VARCHAR NOT NULL, "
                "codec VARCHAR NOT NULL, "
                "language VARCHAR NOT NULL, "
                "normalized_language VARCHAR NOT NULL DEFAULT 'unknown', "
                "manual_language VARCHAR NULL, "
                "match_method VARCHAR NOT NULL DEFAULT 'automatic', "
                "PRIMARY KEY (id), "
                "UNIQUE (relative_path), "
                "CONSTRAINT ck_external_subtitle_match_method "
                "CHECK (match_method IN ('automatic','manual'))"
                ")"
            )
            cursor.execute(
                "INSERT INTO external_subtitles_ownerless_migration "
                "(id, relative_path, codec, language, normalized_language, "
                "manual_language, match_method) "
                "SELECT id, relative_path, codec, language, normalized_language, "
                "manual_language, match_method FROM external_subtitles"
            )
            cursor.execute("DROP TABLE external_subtitles")
            cursor.execute(
                "ALTER TABLE external_subtitles_ownerless_migration "
                "RENAME TO external_subtitles"
            )
            cursor.execute(
                "CREATE INDEX ix_external_subtitles_match_method "
                "ON external_subtitles(match_method)"
            )
            dbapi_connection.commit()
        except BaseException:
            dbapi_connection.rollback()
            raise
        finally:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    finally:
        dbapi_connection.close()

    with engine.connect() as connection:
        violations = list(connection.execute(text("PRAGMA foreign_key_check")))
    if violations:
        raise RuntimeError(
            "Migrace owner-less externích titulků porušila FK invarianty: "
            f"{violations[:3]}"
        )
    logger.info(
        "Migrace databáze: ExternalSubtitle.video_id byl bezpečně odstraněn"
    )
    return True


def _set_catalog_title_structural_values(
    title: CatalogTitle,
    values: AutomaticStructuralInput,
) -> None:
    current = (
        title.part_type,
        title.season_number,
        title.part_number,
        title.season_label,
    )
    if current == values:
        return
    (
        title.part_type,
        title.season_number,
        title.part_number,
        title.season_label,
    ) = values


def _apply_startup_structural_inputs(
    collection: CatalogCollection,
    videos: list[Video],
    inputs: dict[CatalogTitle, AutomaticStructuralInput],
    *,
    infer_final: bool,
) -> None:
    """Apply raw path input or its final shared-inference projection in memory."""
    for title in collection.titles:
        raw = inputs.get(title)
        if raw is None:
            continue
        values = raw
        if infer_final:
            title_videos = [
                video for video in videos
                if video.catalog_title is title
                or video.catalog_title_id == title.id
            ]
            inferred = infer_automatic_structural_values(
                part_type=raw[0],
                season_number=raw[1],
                part_number=raw[2],
                season_label=raw[3],
                is_direct_root=(
                    title.relative_root_path == collection.relative_root_path
                ),
                videos=title_videos,
            )
            values = (
                inferred.part_type,
                inferred.season_number,
                inferred.part_number,
                inferred.season_label,
            )
        _set_catalog_title_structural_values(title, values)


def _migrate_metadata_requirement(connection) -> None:
    existing = {
        column["name"] for column in inspect(connection).get_columns("catalog_titles")
    }
    if "metadata_requirement_manual" not in existing:
        connection.execute(text(
            "ALTER TABLE catalog_titles ADD COLUMN metadata_requirement_manual VARCHAR NULL"
        ))


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
            ("czsk_availability_manual", "VARCHAR NULL"),
            ("catalog_title_id", "INTEGER NULL REFERENCES catalog_titles(id)"),
            (
                "video_variant_group_id",
                "INTEGER NULL REFERENCES video_variant_groups(id) ON DELETE SET NULL",
            ),
            ("catalog_collection_id", "INTEGER NULL REFERENCES catalog_collections(id)"),
            ("local_episode_number", "INTEGER NULL"),
            ("season_episode_number", "INTEGER NULL"),
            ("absolute_episode_number", "INTEGER NULL"),
            ("external_episode_number", "INTEGER NULL"),
            ("episode_number_source", "VARCHAR NOT NULL DEFAULT 'unknown'"),
            ("episode_number_confidence", "FLOAT NULL"),
            ("episode_number_manual_override", "INTEGER NULL"),
            ("recap_episode_number_manual_tenths", "INTEGER NULL"),
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
            ("match_method", "VARCHAR NOT NULL DEFAULT 'automatic'"),
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
        "collection_grouping_decisions": [
            ("target_collection_path", "VARCHAR NULL"),
            ("selected_title_paths_json", "TEXT NULL"),
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
        _migrate_metadata_requirement(connection)
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
            "CREATE INDEX IF NOT EXISTS ix_videos_video_variant_group_id "
            "ON videos(video_variant_group_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_video_variant_groups_catalog_title_id "
            "ON video_variant_groups(catalog_title_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_catalog_collections_hierarchy_status "
            "ON catalog_collections(hierarchy_status)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_external_subtitles_match_method "
            "ON external_subtitles(match_method)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS "
            "ix_external_subtitle_compatibilities_external_subtitle_id "
            "ON external_subtitle_compatibilities(external_subtitle_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS "
            "ix_external_subtitle_compatibilities_video_id "
            "ON external_subtitle_compatibilities(video_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_manual_split_rule_videos_video_id "
            "ON manual_split_rule_videos(video_id)"
        ))

    legacy_external_subtitle_links = _legacy_external_subtitle_links(engine)

    with Session(engine) as session:
        for video in session.scalars(select(Video)):
            video.file_type = classify_video(video.relative_path)
        for subtitle in session.scalars(select(InternalSubtitle)):
            subtitle.normalized_language = normalize_language(subtitle.language, subtitle.title)
        for subtitle in session.scalars(select(ExternalSubtitle)):
            subtitle.normalized_language = normalize_language(subtitle.language)
        created_compatibilities = (
            backfill_legacy_external_subtitle_compatibilities(
                session, legacy_external_subtitle_links
            )
        )
        if created_compatibilities:
            logger.info(
                "Migrace databáze: doplněno %d legacy subtitle compatibility vztahů",
                created_compatibilities,
            )
        consolidated_subtitles = consolidate_legacy_external_subtitle_assets(session)
        if consolidated_subtitles:
            logger.info(
                "Migrace databáze: sloučeno %d historických duplicit fyzických titulků",
                consolidated_subtitles,
            )
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
        grouping_targets = {
            title.relative_root_path: target
            for title, target in collection_grouping_authority_targets(session).items()
        }
        created_automatic_titles: set[CatalogTitle] = set()
        created_automatic_collections: set[CatalogCollection] = set()
        automatic_structural_inputs: dict[
            CatalogTitle, AutomaticStructuralInput
        ] = {}
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
        used_titles.update(
            title for title in original_titles if title.video_variant_groups
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
            title.catalog_collection_id = grouping_targets.get(
                part.relative_root_path, collection,
            ).id
            title.local_title = part.local_title
            title.normalized_local_title = normalize_title(part.local_title)
            if not manual_hierarchy_snapshot_requires_preservation(title):
                automatic_structural_inputs[title] = (
                    part.part_type,
                    part.season_number,
                    part.part_number,
                    part.season_label,
                )
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
                else grouping_targets.get(
                    title.relative_root_path,
                    collections[identity.collection.relative_root_path],
                )
            )
            video.catalog_collection_id = collection.id
            videos_by_collection.setdefault(collection.id, []).append(video)
            # Aktivní manual split se musí nejprve vyhodnotit jako celek.
            # Existující assignment zůstává na videu zachován, ale unassigned
            # video se nesmí před vyhodnocením připojit k prvnímu path title.
            if not manual_split_titles(collection):
                assign_video_catalog_title(video, title)

        apply_collection_grouping_authority(session)
        videos_by_collection = {}
        for video in videos:
            if video.catalog_collection_id is not None:
                videos_by_collection.setdefault(
                    video.catalog_collection_id, []
                ).append(video)

        for collection in collections.values():
            collection_videos = videos_by_collection.get(collection.id, [])
            if not collection_videos:
                _apply_startup_structural_inputs(
                    collection,
                    [],
                    automatic_structural_inputs,
                    infer_final=False,
                )
                continue
            collection.local_period_hint = extract_local_period_hint(collection.local_title)
            if manual_split_titles(collection):
                with session.no_autoflush:
                    _apply_startup_structural_inputs(
                        collection,
                        collection_videos,
                        automatic_structural_inputs,
                        infer_final=False,
                    )
                    manual_split = evaluate_persisted_manual_split(
                        collection,
                        collection_videos,
                    )
                    if historical_manual_split_ambiguities(collection, manual_split):
                        protected_collection_ids.add(collection.id)
                        continue
                    apply_manual_split_decisions(manual_split, collection)
                    _apply_startup_structural_inputs(
                        collection,
                        collection_videos,
                        automatic_structural_inputs,
                        infer_final=True,
                    )
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
                        or title.metadata_requirement_manual is not None
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
                        or title.video_variant_groups
                    ):
                        continue
                    # Startup nejprve odvodí title z fyzické cesty. Pokud však
                    # autoritativní manual split všechna videa přiřadil jinam,
                    # tento automatický prázdný mezivýsledek nesmí přežít sync.
                    session.delete(title)
                    titles.pop(title.relative_root_path, None)
                session.flush()
                continue
            with session.no_autoflush:
                # The raw path projection is an input to the shared inference,
                # not a persistent intermediate state.  Returning to the
                # already stored final value before the next flush keeps a
                # stable startup free of UPDATE/updated_at churn.
                _apply_startup_structural_inputs(
                    collection,
                    collection_videos,
                    automatic_structural_inputs,
                    infer_final=False,
                )
                _apply_startup_structural_inputs(
                    collection,
                    collection_videos,
                    automatic_structural_inputs,
                    infer_final=True,
                )

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
                or title.video_variant_groups
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
            if legacy.metadata_requirement_manual is not None:
                # A retained workflow decision is not a disposable placeholder.
                continue
            has_metadata = session.get(TitleMetadata, legacy.id) is not None
            has_links = session.scalar(select(ExternalTitleLink.id).where(
                ExternalTitleLink.catalog_title_id == legacy.id
            )) is not None
            has_variant_groups = session.scalar(select(VideoVariantGroup.id).where(
                VideoVariantGroup.catalog_title_id == legacy.id
            )) is not None
            if not has_metadata and not has_links and not has_variant_groups:
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

    # Historical schemas constrained only (video_id, relative_path). Run this
    # after the M:N backfill and physical-asset consolidation so old databases
    # receive the same path identity invariant as a fresh schema.
    with engine.begin() as connection:
        inspector = inspect(connection)
        unique_path_exists = any(
            item.get("column_names") == ["relative_path"]
            for item in inspector.get_unique_constraints("external_subtitles")
        ) or any(
            item.get("unique") and item.get("column_names") == ["relative_path"]
            for item in inspector.get_indexes("external_subtitles")
        )
        if not unique_path_exists:
            connection.execute(text(
                "CREATE UNIQUE INDEX ux_external_subtitles_relative_path "
                "ON external_subtitles(relative_path)"
            ))

    _retire_external_subtitle_video_id(engine)


def migrate_schema_at_startup(engine) -> bool:
    """Run compatibility reconstruction once, then keep stable startup read-only.

    Explicit callers of ``migrate_schema`` still request the full idempotent
    reconstruction used by migration/lifecycle tests and maintenance tools.
    The application lifespan uses this version-gated entry point so every
    ordinary restart does not rebuild and transiently rewrite the whole library.
    """
    if engine.dialect.name != "sqlite":
        migrate_schema(engine)
        return True
    with engine.connect() as connection:
        current = int(connection.scalar(text("PRAGMA user_version")) or 0)
    if current >= STARTUP_COMPATIBILITY_VERSION:
        return False
    if current < 1:
        migrate_schema(engine)
    with engine.begin() as connection:
        if current == 1:
            # Preserve current numbering and other derived data after manual
            # UI work; this additive schema upgrade needs no reconstruction.
            _migrate_metadata_requirement(connection)
        connection.execute(text(
            f"PRAGMA user_version = {STARTUP_COMPATIBILITY_VERSION}"
        ))
    return True
