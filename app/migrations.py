from __future__ import annotations

import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from .catalog import classify_video, determine_parent_series, normalize_language, normalize_title
from .database import Base
from .models import CatalogTitle, ExternalSubtitle, InternalSubtitle, Video

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
        ],
        "internal_subtitles": [("normalized_language", "VARCHAR NOT NULL DEFAULT 'unknown'")],
        "external_subtitles": [("normalized_language", "VARCHAR NOT NULL DEFAULT 'unknown'")],
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {column["name"] for column in inspect(connection).get_columns(table)}
            for name, definition in columns:
                if name not in existing:
                    logger.info("Migrace databáze: přidávám %s.%s", table, name)
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))

    with Session(engine) as session:
        for video in session.scalars(select(Video)):
            video.file_type = classify_video(video.relative_path)
        for subtitle in session.scalars(select(InternalSubtitle)):
            subtitle.normalized_language = normalize_language(subtitle.language, subtitle.title)
        for subtitle in session.scalars(select(ExternalSubtitle)):
            subtitle.normalized_language = normalize_language(subtitle.language)
        titles = {
            title.relative_root_path: title
            for title in session.scalars(select(CatalogTitle)).all()
        }
        for video in session.scalars(select(Video).order_by(Video.id)):
            identity = determine_parent_series(video.relative_path)
            title = titles.get(identity.relative_path)
            if title is None:
                title = CatalogTitle(
                    local_title=identity.name,
                    normalized_local_title=normalize_title(identity.name),
                    relative_root_path=identity.relative_path,
                )
                session.add(title)
                session.flush()
                titles[identity.relative_path] = title
            video.catalog_title_id = title.id
        session.commit()
