from datetime import datetime
from hashlib import sha256

from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.orm import Session

from app.migrations import migrate_schema
from app.database import Base
from app.hierarchy_evaluation import HierarchyIssueCode, evaluate_collection_hierarchy
from app.models import (
    AudioTrack, CatalogCollection, CatalogTitle, CollectionGroupingDecision,
    ExternalSubtitle, ExternalSubtitleCompatibility, ExternalTitleLink,
    InternalSubtitle, ManualSplitRuleVideo, TitleMetadata,
    UnresolvedExternalSubtitle, Video,
)
from app.numbering import summarize_title_numbering


def _catalog_title_persisted_state(session: Session):
    semantic_columns = tuple(
        column.name for column in CatalogTitle.__table__.columns
        if column.name not in {"created_at", "updated_at"}
    )
    titles = list(session.scalars(
        select(CatalogTitle).order_by(CatalogTitle.relative_root_path)
    ))
    return (
        {
            title.relative_root_path: tuple(
                getattr(title, column) for column in semantic_columns
            )
            for title in titles
        },
        {title.relative_root_path: title.updated_at for title in titles},
    )


def _record_catalog_title_updates(engine):
    statements = []

    def record(_connection, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE CATALOG_TITLES"):
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    return statements, record


def test_stable_startup_does_not_touch_catalog_titles(tmp_path):
    database_path = tmp_path / "stable-startup.db"
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    paths = (
        "Anime/Flat/Flat - 01.mkv",
        "Anime/Flat/Flat - 02.mkv",
        "Anime/Mixed/Season 1/Mixed - 01.mkv",
        "Anime/Mixed/Season 1/Mixed - 02.mkv",
        "Anime/Mixed/Season 1/OVA/OVA 01.mkv",
        "Anime/Mixed/Season 1/Movies/Movie.mkv",
        "Anime/Manual/Season 3/Manual - 01.mkv",
        "Anime/Manual/Season 3/Manual - 02.mkv",
    )
    with Session(engine) as session:
        session.add_all([
            Video(
                relative_path=path,
                root_folder="Anime",
                filename=path.rsplit("/", 1)[-1],
                size=index,
                mtime_ns=index,
            )
            for index, path in enumerate(paths, 1)
        ])
        session.commit()

    migrate_schema(engine)
    fixed_updated_at = datetime(2024, 1, 2, 3, 4, 5)
    verified_at = datetime(2024, 2, 3, 4, 5, 6)
    with Session(engine) as session:
        manual_season = session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Manual/Season 3"
        ))
        manual_film = session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Mixed/Season 1/Movies"
        ))
        assert manual_season is not None
        assert manual_film is not None
        manual_season.hierarchy_manual_override = True
        manual_season.part_type_manual = "season"
        manual_season.season_number_manual = 3
        manual_season.part_number_manual = None
        manual_season.season_label_manual = "S3"
        manual_season.hierarchy_verified_at = verified_at
        manual_film.hierarchy_manual_override = True
        manual_film.part_type_manual = "film"
        manual_film.season_number_manual = None
        manual_film.part_number_manual = None
        manual_film.season_label_manual = None
        manual_film.hierarchy_verified_at = verified_at
        for title in session.scalars(select(CatalogTitle)):
            title.updated_at = fixed_updated_at
        session.commit()

    # First lifecycle settles derived collection status over the configured
    # representative automatic, supplementary and authoritative hierarchy.
    migrate_schema(engine)
    with Session(engine) as session:
        before_semantic, before_timestamps = _catalog_title_persisted_state(session)
        flat = session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Flat"
        ))
        ova = session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Mixed/Season 1/OVA"
        ))
        manual_season = session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Manual/Season 3"
        ))
        manual_film = session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Mixed/Season 1/Movies"
        ))
        assert (flat.part_type, flat.season_number, flat.season_label) == (
            "season", 1, "S1",
        )
        assert (ova.part_type, ova.season_number, ova.season_label) == (
            "ova", 1, "S1",
        )
        assert manual_season.collection.hierarchy_status == "verified"
        assert (
            manual_season.hierarchy_manual_override,
            manual_season.part_type_manual,
            manual_season.season_number_manual,
            manual_season.season_label_manual,
        ) == (True, "season", 3, "S3")
        assert (
            manual_film.hierarchy_manual_override,
            manual_film.part_type_manual,
            manual_film.season_number_manual,
            manual_film.season_label_manual,
        ) == (True, "film", None, None)
        assert (
            manual_film.part_type,
            manual_film.season_number,
            manual_film.season_label,
            manual_film.effective_season_number,
        ) == ("film", 1, "S1", None)
    before_database_sha = sha256(database_path.read_bytes()).hexdigest()

    updates, listener = _record_catalog_title_updates(engine)
    try:
        migrate_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    with Session(engine) as session:
        after_semantic, after_timestamps = _catalog_title_persisted_state(session)
    assert after_semantic == before_semantic
    assert after_timestamps == before_timestamps
    assert set(after_timestamps.values()) == {fixed_updated_at}
    assert updates == []
    assert sha256(database_path.read_bytes()).hexdigest() == before_database_sha


def test_startup_real_structural_change_updates_title_and_timestamp(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'startup-real-change.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Video(
                relative_path=f"Anime/Flat/E{number:02}.mkv",
                root_folder="Anime",
                filename=f"E{number:02}.mkv",
                size=number,
                mtime_ns=number,
            )
            for number in (1, 2)
        ])
        session.commit()

    migrate_schema(engine)
    fixed_updated_at = datetime(2024, 1, 2, 3, 4, 5)
    with Session(engine) as session:
        title = session.scalar(select(CatalogTitle))
        removed = session.scalar(select(Video).where(Video.filename == "E02.mkv"))
        assert (title.part_type, title.season_number, title.season_label) == (
            "season", 1, "S1",
        )
        title.updated_at = fixed_updated_at
        session.delete(removed)
        session.commit()

    updates, listener = _record_catalog_title_updates(engine)
    try:
        migrate_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    with Session(engine) as session:
        title = session.scalar(select(CatalogTitle))
        assert (title.part_type, title.season_number, title.season_label) == (
            "title", None, None,
        )
        assert title.updated_at != fixed_updated_at
    assert len(updates) == 1


def test_startup_treats_confirmed_duplicate_as_nonblocking_cleanup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'confirmed-cleanup.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
            hierarchy_status="review_required",
            hierarchy_note=(
                "Potvrzené duplicitní soubory vyžadují vyřešení."
            ),
        )
        title = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1",
            part_type="season",
            season_number=1,
            season_label="S1",
            hierarchy_manual_override=True,
            part_type_manual="season",
            season_number_manual=1,
            season_label_manual="S1",
        )
        primary = Video(
            relative_path="Anime/Show/Season 1/Show - 01.mkv",
            root_folder="Anime",
            filename="Show - 01.mkv",
            size=1,
            mtime_ns=1,
            season_episode_number=1,
            catalog_title=title,
            catalog_collection=collection,
        )
        secondary = Video(
            relative_path="Anime/Show/Season 1/Show 01.mp4",
            root_folder="Anime",
            filename="Show 01.mp4",
            size=2,
            mtime_ns=2,
            season_episode_number=1,
            catalog_title=title,
            catalog_collection=collection,
        )
        session.add(collection)
        session.flush()
        secondary.duplicate_of = primary
        session.commit()
        primary_id, secondary_id = primary.id, secondary.id

    migrate_schema(engine)

    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection))
        title = collection.titles[0]
        primary = session.get(Video, primary_id)
        secondary = session.get(Video, secondary_id)
        summary = summarize_title_numbering(list(title.videos), title)
        evaluation = evaluate_collection_hierarchy(
            collection,
            list(collection.videos),
        )
        assert collection.hierarchy_status == "verified"
        assert collection.hierarchy_note is None
        assert (summary.total, summary.standard_total, summary.numbered) == (2, 1, 1)
        assert summary.confirmed_duplicates == 1
        assert secondary.duplicate_of_video_id == primary.id
        confirmed = next(
            issue for issue in evaluation.issues
            if issue.code == HierarchyIssueCode.CONFIRMED_DUPLICATE
        )
        assert confirmed.blocking is False
        assert evaluation.blocking_issues == ()

    migrate_schema(engine)

    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection))
        secondary = session.get(Video, secondary_id)
        assert collection.hierarchy_status == "verified"
        assert collection.hierarchy_note is None
        assert secondary.duplicate_of_video_id == primary_id


def test_startup_flags_existing_duplicate_seasons_without_backfill(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'duplicate-seasons.db'}")
    Base.metadata.create_all(engine)
    verified_at = datetime(2024, 2, 3, 4, 5, 6)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
            hierarchy_status="verified",
        )
        titles = []
        for ordinal, episode in ((1, 1), (2, 14)):
            title = CatalogTitle(
                collection=collection,
                local_title=f"Part {ordinal}",
                normalized_local_title=f"part {ordinal}",
                relative_root_path=f"Anime/Show/Season 1/Part {ordinal}",
                part_type="part",
                season_number=1,
                part_number=ordinal,
                season_label="S1",
                part_type_manual="season",
                season_number_manual=1,
                season_label_manual="S1",
                part_number_manual=None,
                hierarchy_manual_override=True,
                hierarchy_verified_at=verified_at,
            )
            Video(
                relative_path=(
                    f"Anime/Show/Season 1/Part {ordinal}/Episode {episode:02}.mkv"
                ),
                root_folder="Anime",
                filename=f"Episode {episode:02}.mkv",
                size=1,
                mtime_ns=episode,
                catalog_title=title,
                catalog_collection=collection,
            )
            titles.append(title)
        session.add(collection)
        session.commit()
        collection_id = collection.id
        title_ids = [title.id for title in titles]

    migrate_schema(engine)

    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        titles = [session.get(CatalogTitle, title_id) for title_id in title_ids]
        assert collection.hierarchy_status == "review_required"
        assert "Season 1 už v této kolekci existuje" in collection.hierarchy_note
        assert [title.part_type_manual for title in titles] == ["season", "season"]
        assert [title.season_number_manual for title in titles] == [1, 1]
        assert [title.part_number_manual for title in titles] == [None, None]
        assert [title.hierarchy_verified_at for title in titles] == [
            verified_at,
            verified_at,
        ]


def test_startup_sync_applies_shared_direct_root_season_one_inference(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'structural-sync.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for number in range(1, 13):
            session.add(Video(
                relative_path=f"Anime/Show/Show - {number:02}.mkv",
                root_folder="Anime", filename=f"Show - {number:02}.mkv",
                size=1, mtime_ns=number,
            ))
        session.commit()

    migrate_schema(engine)

    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection))
        title = session.scalar(select(CatalogTitle))
        assert (title.part_type, title.season_number, title.season_label) == (
            "season", 1, "S1",
        )
        assert collection.hierarchy_status == "automatic"
        assert collection.hierarchy_verified_at is None


def test_startup_sync_recalculates_unnumbered_ova_without_scan(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'unnumbered-ova-sync.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Video(
            relative_path="Anime/Show/Season 3/Show OVA.mkv",
            root_folder="Anime", filename="Show OVA.mkv", size=1, mtime_ns=1,
            file_type="ova", episode_number_source="unknown",
        ))
        session.commit()

    migrate_schema(engine)

    with Session(engine) as session:
        video = session.scalar(select(Video))
        assert video.file_type == "ova"
        assert video.catalog_title.effective_part_type == "season"
        assert video.catalog_title.effective_season_number == 3
        assert video.local_episode_number is None
        assert video.season_episode_number is None
        assert video.absolute_episode_number is None
        assert video.external_episode_number is None
        assert video.episode_number_source == "supplementary_ova"
        assert video.catalog_collection.hierarchy_status == "automatic"


def test_part_number_manual_migration_is_idempotent_and_preserves_automatic_value(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'part-manual-migration.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Part 2",
            normalized_local_title="part 2",
            relative_root_path="Anime/Show/Part 2", part_type="part",
            season_number=1, part_number=2, season_label="S1",
        )
        session.add(Video(
            relative_path="Anime/Show/Part 2/E01.mkv", root_folder="Anime",
            filename="E01.mkv", size=1, mtime_ns=1, catalog_title=title,
            catalog_collection=collection,
        ))
        session.commit()
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE catalog_titles DROP COLUMN part_number_manual"
        ))

    migrate_schema(engine)
    migrate_schema(engine)

    columns = [column["name"] for column in inspect(engine).get_columns("catalog_titles")]
    assert columns.count("part_number_manual") == 1
    with Session(engine) as session:
        title = session.scalar(select(CatalogTitle))
        assert title.part_number == 2
        assert title.part_number_manual is None


def test_media_part_number_migration_is_idempotent_and_does_not_infer_values(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'media-part-migration.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Video(
            relative_path="Anime/Movie/Movie P1.mkv", root_folder="Anime",
            filename="Movie P1.mkv", size=1, mtime_ns=1,
        ))
        session.commit()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE videos DROP COLUMN media_part_number"))

    migrate_schema(engine)
    migrate_schema(engine)

    columns = [column["name"] for column in inspect(engine).get_columns("videos")]
    assert columns.count("media_part_number") == 1
    with Session(engine) as session:
        item = session.scalar(select(Video))
        assert item.media_part_number is None
        item.media_part_number = 2
        session.commit()

    migrate_schema(engine)
    migrate_schema(engine)
    with Session(engine) as session:
        assert session.scalar(select(Video.media_part_number)) == 2


def test_manual_split_authority_migration_is_idempotent_and_does_not_backfill_assignment(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual-split-authority.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection,
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        video = Video(
            relative_path="Anime/Show/E01.mkv",
            root_folder="Anime",
            filename="E01.mkv",
            size=1,
            mtime_ns=1,
            catalog_title=title,
            catalog_collection=collection,
        )
        session.add(video)
        session.commit()
        video_id = video.id
        title_id = title.id

    # Simuluje DB z checkpointu před Commit 4A. Výsledný assignment už existuje,
    # ale samostatná manual-split authority tabulka ještě ne.
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE manual_split_rule_videos"))

    migrate_schema(engine)

    inspector = inspect(engine)
    assert inspector.get_table_names().count("manual_split_rule_videos") == 1
    assert inspector.get_pk_constraint("manual_split_rule_videos")[
        "constrained_columns"
    ] == ["catalog_title_id", "video_id"]
    foreign_keys = {
        constraint["constrained_columns"][0]: constraint
        for constraint in inspector.get_foreign_keys("manual_split_rule_videos")
    }
    assert {
        column: (
            constraint["referred_table"],
            constraint["referred_columns"],
            constraint["options"].get("ondelete"),
        )
        for column, constraint in foreign_keys.items()
    } == {
        "catalog_title_id": ("catalog_titles", ["id"], "CASCADE"),
        "video_id": ("videos", ["id"], "CASCADE"),
    }
    assert any(
        index["name"] == "ix_manual_split_rule_videos_video_id"
        and index["column_names"] == ["video_id"]
        and not index["unique"]
        for index in inspector.get_indexes("manual_split_rule_videos")
    )

    with Session(engine) as session:
        video = session.get(Video, video_id)
        assert video.catalog_title_id == title_id
        assert session.scalar(
            select(func.count()).select_from(ManualSplitRuleVideo)
        ) == 0
        assert list(session.execute(text("PRAGMA foreign_key_check"))) == []

    migrate_schema(engine)

    assert inspect(engine).get_table_names().count("manual_split_rule_videos") == 1
    with Session(engine) as session:
        video = session.get(Video, video_id)
        assert video.catalog_title_id == title_id
        assert session.scalar(
            select(func.count()).select_from(ManualSplitRuleVideo)
        ) == 0


def test_manual_split_authority_migration_preserves_historical_manual_snapshot(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual-snapshot-authority.db'}")
    Base.metadata.create_all(engine)
    verified_at = datetime(2024, 1, 2, 3, 4, 5)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
            hierarchy_status="verified",
            hierarchy_verified_at=verified_at,
        )
        title = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show",
            hierarchy_manual_override=True,
            part_type_manual="season",
            season_number_manual=1,
            season_label_manual="S1",
            part_number_manual=None,
            sort_order_manual=7,
            hierarchy_verified_at=verified_at,
            episode_start=None,
            episode_end=None,
            episode_start_offset=None,
            numbering_mode="season_local",
            numbering_manual=True,
        )
        video = Video(
            relative_path="Anime/Show/E01.mkv",
            root_folder="Anime",
            filename="E01.mkv",
            size=1,
            mtime_ns=1,
            catalog_title=title,
            catalog_collection=collection,
        )
        session.add(video)
        session.commit()
        collection_id = collection.id
        title_id = title.id
        video_id = video.id

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE manual_split_rule_videos"))

    expected_title_snapshot = (
        True,
        "season",
        1,
        "S1",
        None,
        7,
        verified_at,
        None,
        None,
        None,
        "season_local",
        True,
    )

    for _ in range(2):
        migrate_schema(engine)
        with Session(engine) as session:
            collection = session.get(CatalogCollection, collection_id)
            title = session.get(CatalogTitle, title_id)
            video = session.get(Video, video_id)
            assert (
                title.hierarchy_manual_override,
                title.part_type_manual,
                title.season_number_manual,
                title.season_label_manual,
                title.part_number_manual,
                title.sort_order_manual,
                title.hierarchy_verified_at,
                title.episode_start,
                title.episode_end,
                title.episode_start_offset,
                title.numbering_mode,
                title.numbering_manual,
            ) == expected_title_snapshot
            assert collection.hierarchy_status == "verified"
            assert collection.hierarchy_verified_at == verified_at
            assert video.catalog_title_id == title_id
            assert video.catalog_collection_id == collection_id
            assert session.scalar(
                select(func.count()).select_from(ManualSplitRuleVideo)
            ) == 0


def test_startup_sync_preserves_empty_ui_review_collection_and_manual_title(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty-protected-sync.db'}")
    Base.metadata.create_all(engine)
    verified_at = datetime(2024, 2, 3, 4, 5, 6)
    expected_collection = (
        "review_required",
        "UI review note must remain authoritative",
        verified_at,
        "user preserved normalized name",
        "L20-P23",
    )
    expected_title = (
        True,
        "season",
        1,
        None,
        "S1",
        1,
        verified_at,
    )
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Protected Show (L20-P23)",
            normalized_local_title=expected_collection[3],
            relative_root_path="Anime/Protected Show",
            hierarchy_status=expected_collection[0],
            hierarchy_note=expected_collection[1],
            hierarchy_verified_at=verified_at,
            local_period_hint=expected_collection[4],
        )
        title = CatalogTitle(
            collection=collection,
            local_title="Manual Season 1",
            normalized_local_title="manual season one preserved",
            relative_root_path="Anime/Protected Show/.manual-season-1",
            part_type="season",
            season_number=1,
            season_label="S1",
            sort_order=1,
            hierarchy_manual_override=True,
            part_type_manual="season",
            season_number_manual=1,
            part_number_manual=None,
            season_label_manual="S1",
            sort_order_manual=1,
            hierarchy_verified_at=verified_at,
        )
        session.add_all([collection, title])
        session.commit()
        collection_id, title_id = collection.id, title.id

    observed = []
    for _ in range(2):
        migrate_schema(engine)
        with Session(engine) as session:
            collection = session.get(CatalogCollection, collection_id)
            title = session.get(CatalogTitle, title_id)
            assert collection is not None
            assert title is not None
            collection_snapshot = (
                collection.hierarchy_status,
                collection.hierarchy_note,
                collection.hierarchy_verified_at,
                collection.normalized_local_title,
                collection.local_period_hint,
            )
            title_snapshot = (
                title.hierarchy_manual_override,
                title.part_type_manual,
                title.season_number_manual,
                title.part_number_manual,
                title.season_label_manual,
                title.sort_order_manual,
                title.hierarchy_verified_at,
            )
            assert collection_snapshot == expected_collection
            assert title_snapshot == expected_title
            assert title.catalog_collection_id == collection_id
            assert session.scalar(select(func.count()).select_from(Video)) == 0
            observed.append((collection_snapshot, title_snapshot))

    assert observed[0] == observed[1]


def test_startup_sync_preserves_nested_parent_season_for_part(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nested-part-sync.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Video(
            relative_path="Anime/Show/Season 1/Part 2/E01.mkv",
            root_folder="Anime", filename="E01.mkv", size=1, mtime_ns=1,
        ))
        session.commit()

    migrate_schema(engine)

    with Session(engine) as session:
        title = session.scalar(select(CatalogTitle))
        assert title.part_type == "part"
        assert title.season_number == 1
        assert title.part_number == 2
        assert title.season_label == "S1"
        assert title.part_number_manual is None


def test_grouping_authority_columns_are_added_idempotently_to_legacy_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-grouping.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE collection_grouping_decisions"))
        connection.execute(text("""
            CREATE TABLE collection_grouping_decisions (
                id INTEGER PRIMARY KEY,
                suggestion_key VARCHAR NOT NULL UNIQUE,
                state_fingerprint VARCHAR NOT NULL,
                decision VARCHAR NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            )
        """))
        connection.execute(text("""
            INSERT INTO collection_grouping_decisions
                (id, suggestion_key, state_fingerprint, decision)
            VALUES (1, 'legacy', 'state', 'merged')
        """))

    migrate_schema(engine)
    migrate_schema(engine)

    columns = [
        column["name"]
        for column in inspect(engine).get_columns("collection_grouping_decisions")
    ]
    assert columns.count("target_collection_path") == 1
    assert columns.count("selected_title_paths_json") == 1
    with Session(engine) as session:
        decision = session.get(CollectionGroupingDecision, 1)
        assert decision.decision == "merged"
        assert decision.target_collection_path is None
        assert decision.selected_title_paths_json is None


def test_migrates_existing_database_and_backfills_values(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY, relative_path VARCHAR UNIQUE NOT NULL,
                root_folder VARCHAR NOT NULL, filename VARCHAR NOT NULL,
                size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                duration FLOAT, video_codec VARCHAR, width INTEGER, height INTEGER
            )
        """))
        connection.execute(text("""
            CREATE TABLE audio_tracks (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
                stream_index INTEGER NOT NULL, codec VARCHAR,
                language VARCHAR NOT NULL,
                UNIQUE(video_id, stream_index)
            )
        """))
        connection.execute(text("""
            CREATE TABLE internal_subtitles (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
                stream_index INTEGER NOT NULL, codec VARCHAR,
                language VARCHAR NOT NULL, title VARCHAR,
                UNIQUE(video_id, stream_index)
            )
        """))
        connection.execute(text("""
            CREATE TABLE external_subtitles (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
                relative_path VARCHAR NOT NULL, codec VARCHAR NOT NULL,
                language VARCHAR NOT NULL,
                UNIQUE(video_id, relative_path)
            )
        """))
        connection.execute(text("""
            INSERT INTO videos
                (id, relative_path, root_folder, filename, size, mtime_ns,
                 duration, video_codec, width, height)
            VALUES
                (1, 'Show/NCOP.mkv', 'Show', 'NCOP.mkv', 987, 654,
                 123.5, 'h265', 1280, 720)
        """))
        connection.execute(text("""
            INSERT INTO audio_tracks
                (id, video_id, stream_index, codec, language)
            VALUES (1, 1, 1, 'aac', 'unknown')
        """))
        connection.execute(text("""
            INSERT INTO internal_subtitles
                (id, video_id, stream_index, codec, language, title)
            VALUES (1, 1, 2, 'ass', 'unknown', 'English (UK)')
        """))
        connection.execute(text("""
            INSERT INTO external_subtitles
                (id, video_id, relative_path, codec, language)
            VALUES (1, 1, 'Show/NCOP.eng.srt', 'srt', 'eng')
        """))

    migrate_schema(engine)

    assert "collection_grouping_decisions" in inspect(engine).get_table_names()
    assert "unresolved_external_subtitles" in inspect(engine).get_table_names()
    assert "external_subtitle_compatibilities" in inspect(engine).get_table_names()

    assert [
        column["name"] for column in inspect(engine).get_columns("videos")
    ].count("content_type_manual") == 1
    assert [
        column["name"] for column in inspect(engine).get_columns("videos")
    ].count("duplicate_of_video_id") == 1
    assert [
        column["name"] for column in inspect(engine).get_columns("videos")
    ].count("duplicate_primary_missing") == 1
    assert [
        column["name"] for column in inspect(engine).get_columns("videos")
    ].count("duplicate_status_manual") == 1
    assert [
        column["name"] for column in inspect(engine).get_columns("videos")
    ].count("media_part_number") == 1
    assert [
        column["name"] for column in inspect(engine).get_columns("videos")
    ].count("czsk_availability_manual") == 1

    with Session(engine) as session:
        assert session.scalar(select(Video.file_type)) == "ncop"
        video = session.scalar(select(Video))
        assert (
            video.id, video.relative_path, video.root_folder, video.filename,
            video.size, video.mtime_ns, video.duration, video.video_codec,
            video.width, video.height,
        ) == (
            1, "Show/NCOP.mkv", "Show", "NCOP.mkv",
            987, 654, 123.5, "h265", 1280, 720,
        )
        assert video.content_type_manual is None
        assert video.media_part_number is None
        assert video.czsk_availability_manual is None
        assert video.duplicate_status_manual is None
        assert video.duplicate_of_video_id is None
        assert video.duplicate_primary_missing is False
        assert video.manual_hardsub_cs is False
        assert video.manual_hardsub_sk is False
        assert video.manual_hardsub_verified_at is None
        audio_track = session.scalar(select(AudioTrack))
        assert audio_track.language == "unknown"
        assert audio_track.manual_language is None
        audio_track.manual_language = "ja"
        assert session.scalar(select(InternalSubtitle.language)) == "unknown"
        assert session.scalar(select(InternalSubtitle.normalized_language)) == "en"
        subtitle = session.scalar(select(ExternalSubtitle))
        assert subtitle.normalized_language == "en"
        assert subtitle.manual_language is None
        compatibility = session.scalar(select(ExternalSubtitleCompatibility))
        assert (
            compatibility.external_subtitle_id,
            compatibility.video_id,
            compatibility.status,
            compatibility.match_method,
            compatibility.verified_at,
        ) == (
            subtitle.id,
            video.id,
            "automatic_match",
            "legacy_backfill",
            None,
        )
        subtitle.manual_language = "cs"
        video.content_type_manual = "recap"
        video.duplicate_status_manual = "suspected"
        video.czsk_availability_manual = "unavailable"
        session.add(CollectionGroupingDecision(
            suggestion_key="test", state_fingerprint="state", decision="separate",
        ))
        session.commit()

    migrate_schema(engine)
    migrate_schema(engine)

    assert [
        column["name"] for column in inspect(engine).get_columns("videos")
    ].count("content_type_manual") == 1
    assert [
        column["name"] for column in inspect(engine).get_columns("videos")
    ].count("duplicate_status_manual") == 1
    assert [
        column["name"] for column in inspect(engine).get_columns("videos")
    ].count("czsk_availability_manual") == 1
    assert [
        column["name"] for column in inspect(engine).get_columns("audio_tracks")
    ].count("manual_language") == 1
    assert [
        column["name"] for column in inspect(engine).get_columns("external_subtitles")
    ].count("manual_language") == 1
    assert [
        column["name"] for column in inspect(engine).get_columns("external_subtitles")
    ].count("match_method") == 1
    with Session(engine) as session:
        video = session.scalar(select(Video))
        assert video.content_type_manual == "recap"
        assert video.duplicate_status_manual == "suspected"
        assert video.czsk_availability_manual == "unavailable"
        assert session.scalar(select(CollectionGroupingDecision)).decision == "separate"
        assert session.scalar(select(AudioTrack.manual_language)) == "ja"
        assert session.scalar(select(ExternalSubtitle.manual_language)) == "cs"
        assert session.scalar(select(ExternalSubtitle.match_method)) == "automatic"
        session.add(UnresolvedExternalSubtitle(
            relative_path="Show/orphan.ass", filename="orphan.ass",
            extension=".ass", status="confirmed_no_match",
        ))
        session.commit()
        assert (
            video.relative_path, video.filename, video.size, video.mtime_ns,
            video.duration, video.video_codec, video.width, video.height,
        ) == (
            "Show/NCOP.mkv", "NCOP.mkv", 987, 654,
            123.5, "h265", 1280, 720,
        )

    subtitle_inspector = inspect(engine)
    subtitle_indexes = subtitle_inspector.get_indexes("external_subtitles")
    subtitle_uniques = subtitle_inspector.get_unique_constraints(
        "external_subtitles"
    )
    assert any(
        item["column_names"] == ["relative_path"]
        for item in subtitle_uniques
    ) or any(
        item["unique"] and item["column_names"] == ["relative_path"]
        for item in subtitle_indexes
    )
    assert "video_id" not in {
        column["name"]
        for column in subtitle_inspector.get_columns("external_subtitles")
    }

    migrate_schema(engine)
    with Session(engine) as session:
        unresolved = session.scalar(select(UnresolvedExternalSubtitle))
        assert (unresolved.relative_path, unresolved.status) == (
            "Show/orphan.ass", "confirmed_no_match",
        )


def test_migration_consolidates_historical_physical_path_duplicates(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-subtitle-assets.db'}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY, relative_path VARCHAR UNIQUE NOT NULL,
                root_folder VARCHAR NOT NULL, filename VARCHAR NOT NULL,
                size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                duration FLOAT, video_codec VARCHAR, width INTEGER, height INTEGER
            )
        """))
        connection.execute(text("""
            CREATE TABLE external_subtitles (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
                relative_path VARCHAR NOT NULL, codec VARCHAR NOT NULL,
                language VARCHAR NOT NULL,
                UNIQUE(video_id, relative_path)
            )
        """))
        connection.execute(text("""
            INSERT INTO videos
                (id, relative_path, root_folder, filename, size, mtime_ns)
            VALUES
                (1, 'Show/Show - 01.mkv', 'Show', 'Show - 01.mkv', 1, 1),
                (2, 'Show/Show - 01 TV.mkv', 'Show', 'Show - 01 TV.mkv', 2, 2)
        """))
        connection.execute(text("""
            INSERT INTO external_subtitles
                (id, video_id, relative_path, codec, language)
            VALUES
                (1, 1, 'Show/Show - 01.ass', 'ass', 'cze'),
                (2, 2, 'Show/Show - 01.ass', 'ass', 'cze')
        """))

    migrate_schema(engine)
    migrate_schema(engine)

    with Session(engine) as session:
        assets = list(session.scalars(select(ExternalSubtitle)))
        rows = list(session.scalars(
            select(ExternalSubtitleCompatibility).order_by(
                ExternalSubtitleCompatibility.video_id
            )
        ))
        assert len(assets) == 1
        assert assets[0].relative_path == "Show/Show - 01.ass"
        assert [(row.video_id, row.status, row.match_method) for row in rows] == [
            (1, "automatic_match", "legacy_backfill"),
            (2, "automatic_match", "legacy_backfill"),
        ]
        assert {row.external_subtitle_id for row in rows} == {assets[0].id}
    subtitle_inspector = inspect(engine)
    assert any(
        item["column_names"] == ["relative_path"]
        for item in subtitle_inspector.get_unique_constraints(
            "external_subtitles"
        )
    ) or any(
        item["unique"] and item["column_names"] == ["relative_path"]
        for item in subtitle_inspector.get_indexes("external_subtitles")
    )


def test_duplicate_relation_migration_is_idempotent_and_preserves_old_video_data(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre-duplicates.db'}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY, relative_path VARCHAR UNIQUE NOT NULL,
                root_folder VARCHAR NOT NULL, filename VARCHAR NOT NULL,
                size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                duration FLOAT, video_codec VARCHAR, width INTEGER, height INTEGER
            )
        """))
        connection.execute(text("""
            INSERT INTO videos
                (id, relative_path, root_folder, filename, size, mtime_ns,
                 duration, video_codec, width, height)
            VALUES
                (1, 'Show/Show - 01.mkv', 'Show', 'Show - 01.mkv', 100, 11,
                 1200, 'h264', 1920, 1080),
                (2, 'Show/Show 01.mp4', 'Show', 'Show 01.mp4', 200, 22,
                 1201, 'h265', 1280, 720)
        """))

    migrate_schema(engine)
    columns = [column["name"] for column in inspect(engine).get_columns("videos")]
    assert columns.count("duplicate_of_video_id") == 1
    assert columns.count("duplicate_primary_missing") == 1
    assert columns.count("duplicate_status_manual") == 1
    with Session(engine) as session:
        videos = list(session.scalars(select(Video).order_by(Video.id)))
        assert [(video.relative_path, video.size, video.video_codec) for video in videos] == [
            ("Show/Show - 01.mkv", 100, "h264"),
            ("Show/Show 01.mp4", 200, "h265"),
        ]
        assert all(video.duplicate_of_video_id is None for video in videos)
        assert all(video.duplicate_primary_missing is False for video in videos)
        assert all(video.duplicate_status_manual is None for video in videos)
        videos[1].duplicate_of = videos[0]
        session.commit()

    migrate_schema(engine)
    migrate_schema(engine)

    columns = [column["name"] for column in inspect(engine).get_columns("videos")]
    assert columns.count("duplicate_of_video_id") == 1
    assert columns.count("duplicate_status_manual") == 1
    with Session(engine) as session:
        primary, duplicate = session.scalars(select(Video).order_by(Video.id)).all()
        assert duplicate.duplicate_of_video_id == primary.id
        assert duplicate.duplicate_of is primary
        assert (primary.filename, duplicate.filename) == (
            "Show - 01.mkv", "Show 01.mp4",
        )


def test_v5_migration_creates_stable_titles_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'v4.db'}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY, relative_path VARCHAR UNIQUE NOT NULL,
                root_folder VARCHAR NOT NULL, filename VARCHAR NOT NULL,
                size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                duration FLOAT, video_codec VARCHAR, width INTEGER, height INTEGER,
                file_type VARCHAR NOT NULL DEFAULT 'other',
                manual_hardsub_cs BOOLEAN NOT NULL DEFAULT 0,
                manual_hardsub_sk BOOLEAN NOT NULL DEFAULT 0,
                manual_hardsub_verified_at DATETIME
            )
        """))
        connection.execute(text("""
            CREATE TABLE internal_subtitles (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
                stream_index INTEGER NOT NULL, codec VARCHAR, language VARCHAR NOT NULL,
                normalized_language VARCHAR NOT NULL DEFAULT 'unknown', title VARCHAR,
                UNIQUE(video_id, stream_index)
            )
        """))
        connection.execute(text("""
            CREATE TABLE external_subtitles (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
                relative_path VARCHAR NOT NULL, codec VARCHAR NOT NULL,
                language VARCHAR NOT NULL, normalized_language VARCHAR NOT NULL DEFAULT 'unknown',
                UNIQUE(video_id, relative_path)
            )
        """))
        connection.execute(text("""
            INSERT INTO videos (id, relative_path, root_folder, filename, size, mtime_ns,
              manual_hardsub_cs, manual_hardsub_sk)
            VALUES
              (1, 'Anime/Show/Season 01/E01.mkv', 'Anime', 'E01.mkv', 1, 1, 1, 0),
              (2, 'Anime/Show/Season 02/E01.mkv', 'Anime', 'E01.mkv', 1, 1, 0, 1),
              (3, 'Anime/Other/E01.mkv', 'Anime', 'E01.mkv', 1, 1, 0, 0)
        """))

    migrate_schema(engine)
    migrate_schema(engine)

    with Session(engine) as session:
        titles = session.scalars(select(CatalogTitle).order_by(CatalogTitle.id)).all()
        videos = session.scalars(select(Video).order_by(Video.id)).all()
        assert len(titles) == 3
        assert session.scalar(select(func.count()).select_from(CatalogCollection)) == 2
        assert titles[0].metadata_status == "unlinked"
        assert all(video.catalog_title_id is not None for video in videos)
        assert videos[0].catalog_title_id != videos[1].catalog_title_id
        assert videos[0].catalog_title.season_label == "S1"
        assert videos[1].catalog_title.season_label == "S2"
        assert videos[0].manual_hardsub_cs is True
        assert videos[1].manual_hardsub_sk is True


def test_collection_migration_splits_ansatsu_and_overlord_idempotently(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hierarchy.db'}")
    Base.metadata.create_all(engine)
    paths = [
        "Anime/Ansatsu Kyoushitsu (Z15-Z16)/Serie 1 (Z15)/E01.mkv",
        "Anime/Ansatsu Kyoushitsu (Z15-Z16)/Serie 2 (Z16)/E01.mkv",
        *[
            f"Anime/OVERLORD (L15-L22)/OVERLORD {roman}/E01.mkv"
            for roman in ("I", "II", "III", "IV")
        ],
    ]
    with Session(engine) as session:
        for index, path in enumerate(paths, 1):
            session.add(Video(
                relative_path=path, root_folder="Anime", filename="E01.mkv",
                size=index, mtime_ns=index, manual_hardsub_cs=index == 1,
            ))
        session.commit()

    migrate_schema(engine)
    migrate_schema(engine)

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CatalogCollection)) == 2
        assert session.scalar(select(func.count()).select_from(CatalogTitle)) == 6
        assert session.scalar(select(func.count()).select_from(Video).where(
            Video.catalog_title_id.is_not(None)
        )) == 6
        ansatsu = session.scalar(select(CatalogCollection).where(
            CatalogCollection.local_title.like("Ansatsu%")
        ))
        assert [title.season_number for title in sorted(ansatsu.titles, key=lambda x: x.sort_order)] == [1, 2]
        assert session.scalar(select(Video).where(Video.manual_hardsub_cs.is_(True))).manual_hardsub_cs


def test_ambiguous_legacy_metadata_is_preserved_for_review(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'review.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        legacy = CatalogTitle(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", metadata_status="linked_manual",
        )
        session.add(legacy)
        session.flush()
        session.add_all([
            Video(relative_path=f"Anime/Show/Season 0{number}/E01.mkv", root_folder="Anime",
                  filename="E01.mkv", size=number, mtime_ns=number, catalog_title_id=legacy.id)
            for number in (1, 2)
        ])
        session.add(ExternalTitleLink(
            catalog_title_id=legacy.id, provider="anilist", external_id="123",
            match_method="manual_search", is_primary=True, is_manual=True,
        ))
        session.add(TitleMetadata(
            catalog_title_id=legacy.id, display_title="Remote Show",
            metadata_provider="anilist", metadata_external_id="123",
        ))
        session.commit()
        legacy_id = legacy.id

    migrate_schema(engine)

    with Session(engine) as session:
        legacy = session.get(CatalogTitle, legacy_id)
        assert legacy.metadata_status == "migration_review_required"
        assert session.get(TitleMetadata, legacy_id).display_title == "Remote Show"
        assert session.scalar(select(ExternalTitleLink).where(
            ExternalTitleLink.catalog_title_id == legacy_id
        )).external_id == "123"
        assert session.scalar(select(func.count()).select_from(Video)) == 2


def test_startup_final_evaluation_sees_preserved_legacy_title(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-timing.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        legacy = CatalogTitle(
            local_title="Legacy Show",
            normalized_local_title="legacy show",
            relative_root_path="Anime/Show",
            metadata_status="linked_manual",
        )
        session.add(legacy)
        session.flush()
        session.add_all([
            Video(
                relative_path=f"Anime/Show/Season {number}/E01.mkv",
                root_folder="Anime",
                filename="E01.mkv",
                size=number,
                mtime_ns=number,
                catalog_title_id=legacy.id,
            )
            for number in (1, 2)
        ])
        session.add(TitleMetadata(
            catalog_title_id=legacy.id,
            display_title="Remote Show",
            metadata_provider="anilist",
            metadata_external_id="legacy-123",
        ))
        session.commit()

    from app import migrations as migrations_module

    observed_part_types: list[tuple[str, ...]] = []
    original_finalize = migrations_module.finalize_collection_hierarchy

    def record_final_titles(collection, videos, **kwargs):
        observed_part_types.append(tuple(sorted(
            title.part_type for title in collection.titles
        )))
        return original_finalize(collection, videos, **kwargs)

    monkeypatch.setattr(
        migrations_module,
        "finalize_collection_hierarchy",
        record_final_titles,
    )

    migrate_schema(engine)

    assert observed_part_types
    assert any("migration_review" in values for values in observed_part_types)


def test_migration_preserves_3098_videos_hardsubs_subtitles_and_manual_hierarchy(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'production-shape.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show", relative_root_path="Anime/Show",
            hierarchy_status="verified",
        )
        title = CatalogTitle(
            local_title="Show", normalized_local_title="show", relative_root_path="Anime/Show",
            collection=collection, hierarchy_manual_override=True,
            season_number_manual=1, season_label_manual="S1",
        )
        session.add(title)
        session.flush()
        videos = [Video(
            relative_path=f"Anime/Show/E{number:04d}.mkv", root_folder="Anime",
            filename=f"E{number:04d}.mkv", size=number, mtime_ns=number,
            catalog_title_id=title.id, catalog_collection_id=collection.id,
            manual_hardsub_cs=number == 1,
        ) for number in range(1, 3099)]
        session.add_all(videos)
        session.flush()
        session.add(InternalSubtitle(video_id=videos[0].id, stream_index=1, language="cze", normalized_language="cs"))
        session.commit()

    migrate_schema(engine)

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Video)) == 3098
        assert session.scalar(select(Video.manual_hardsub_cs).where(Video.id == 1)) is True
        assert session.scalar(select(func.count()).select_from(InternalSubtitle)) == 1
        title = session.scalar(select(CatalogTitle).where(CatalogTitle.relative_root_path == "Anime/Show"))
        assert title.season_number_manual == 1
        assert title.season_label_manual == "S1"
        assert title.hierarchy_manual_override is True
