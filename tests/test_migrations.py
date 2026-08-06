from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.migrations import migrate_schema
from app.database import Base
from app.models import (
    CatalogCollection, CatalogTitle, ExternalTitleLink, InternalSubtitle,
    TitleMetadata, Video,
)


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
                (id, relative_path, root_folder, filename, size, mtime_ns)
            VALUES (1, 'Show/NCOP.mkv', 'Show', 'NCOP.mkv', 1, 1)
        """))
        connection.execute(text("""
            INSERT INTO internal_subtitles
                (id, video_id, stream_index, codec, language, title)
            VALUES (1, 1, 2, 'ass', 'unknown', 'English (UK)')
        """))

    migrate_schema(engine)

    with Session(engine) as session:
        assert session.scalar(select(Video.file_type)) == "ncop"
        video = session.scalar(select(Video))
        assert video.filename == "NCOP.mkv"
        assert video.manual_hardsub_cs is False
        assert video.manual_hardsub_sk is False
        assert video.manual_hardsub_verified_at is None
        assert session.scalar(select(InternalSubtitle.language)) == "unknown"
        assert session.scalar(select(InternalSubtitle.normalized_language)) == "eng"


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
