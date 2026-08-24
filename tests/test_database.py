import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app.models import (
    CatalogCollection,
    CatalogTitle,
    ManualSplitRuleVideo,
    Video,
)


def _engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'foreign-keys.db'}")
    Base.metadata.create_all(engine)
    return engine


def _authority_rows(engine):
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1",
            part_type="season",
            season_number=1,
        )
        video = Video(
            relative_path="Anime/Show/E01.mkv",
            root_folder="Anime",
            filename="E01.mkv",
            size=1,
            mtime_ns=1,
            catalog_collection=collection,
        )
        session.add_all([
            collection,
            video,
            ManualSplitRuleVideo(catalog_title=title, video=video),
        ])
        session.commit()
        return title.id, video.id


def test_make_engine_enables_sqlite_foreign_keys_on_every_connection(tmp_path):
    engine = _engine(tmp_path)

    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1

    engine.dispose()
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO manual_split_rule_videos "
                "(catalog_title_id, video_id) VALUES (999, 999)"
            ))


def test_video_delete_cascades_manual_split_authority(tmp_path):
    engine = _engine(tmp_path)
    _title_id, video_id = _authority_rows(engine)

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM videos WHERE id = :video_id"),
            {"video_id": video_id},
        )

    with Session(engine) as session:
        assert session.scalars(select(ManualSplitRuleVideo)).all() == []


def test_title_delete_cascades_manual_split_authority(tmp_path):
    engine = _engine(tmp_path)
    title_id, video_id = _authority_rows(engine)

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM catalog_titles WHERE id = :title_id"),
            {"title_id": title_id},
        )

    with Session(engine) as session:
        assert session.scalars(select(ManualSplitRuleVideo)).all() == []
        assert session.get(Video, video_id) is not None
