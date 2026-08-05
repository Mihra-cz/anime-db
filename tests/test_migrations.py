from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.migrations import migrate_schema
from app.models import InternalSubtitle, Video


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
