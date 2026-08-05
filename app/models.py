from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    relative_path: Mapped[str] = mapped_column(String, unique=True, index=True)
    root_folder: Mapped[str] = mapped_column(String, index=True)
    filename: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    mtime_ns: Mapped[int] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_type: Mapped[str] = mapped_column(String, default="other", server_default="other", index=True)
    manual_hardsub_cs: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    manual_hardsub_sk: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    manual_hardsub_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    catalog_title_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_titles.id"), nullable=True, index=True
    )

    audio_tracks: Mapped[list[AudioTrack]] = relationship(cascade="all, delete-orphan")
    internal_subtitles: Mapped[list[InternalSubtitle]] = relationship(cascade="all, delete-orphan")
    external_subtitles: Mapped[list[ExternalSubtitle]] = relationship(cascade="all, delete-orphan")
    catalog_title: Mapped[CatalogTitle | None] = relationship(back_populates="videos")


METADATA_STATUSES = (
    "unlinked", "candidates_available", "linked_auto", "linked_manual",
    "conflict", "unavailable", "error",
)


class CatalogTitle(Base):
    __tablename__ = "catalog_titles"
    id: Mapped[int] = mapped_column(primary_key=True)
    local_title: Mapped[str] = mapped_column(String, nullable=False)
    normalized_local_title: Mapped[str] = mapped_column(String, nullable=False, index=True)
    relative_root_path: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    manual_display_title: Mapped[str | None] = mapped_column(String, nullable=True)
    preferred_metadata_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    preferred_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_status: Mapped[str] = mapped_column(String, default="unlinked", server_default="unlinked")
    metadata_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    videos: Mapped[list[Video]] = relationship(back_populates="catalog_title")
    external_links: Mapped[list[ExternalTitleLink]] = relationship(cascade="all, delete-orphan")
    metadata_record: Mapped[TitleMetadata | None] = relationship(cascade="all, delete-orphan")
    __table_args__ = (CheckConstraint(
        "metadata_status IN ('unlinked','candidates_available','linked_auto','linked_manual','conflict','unavailable','error')",
        name="ck_catalog_title_metadata_status",
    ),)


class ExternalTitleLink(Base):
    __tablename__ = "external_title_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    catalog_title_id: Mapped[int] = mapped_column(ForeignKey("catalog_titles.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    external_url: Mapped[str | None] = mapped_column(String, nullable=True)
    match_method: Mapped[str] = mapped_column(String, nullable=False)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("catalog_title_id", "provider", "external_id"),)


class TitleMetadata(Base):
    __tablename__ = "title_metadata"
    catalog_title_id: Mapped[int] = mapped_column(ForeignKey("catalog_titles.id", ondelete="CASCADE"), primary_key=True)
    display_title: Mapped[str] = mapped_column(String, nullable=False)
    title_romaji: Mapped[str | None] = mapped_column(String, nullable=True)
    title_english: Mapped[str | None] = mapped_column(String, nullable=True)
    title_native: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[str | None] = mapped_column(String, nullable=True)
    format: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    episode_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    genres_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    tags_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    synonyms_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    country_of_origin: Mapped[str | None] = mapped_column(String, nullable=True)
    is_adult: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    metadata_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AudioTrack(Base):
    __tablename__ = "audio_tracks"
    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    stream_index: Mapped[int] = mapped_column(Integer)
    codec: Mapped[str | None] = mapped_column(String, nullable=True)
    language: Mapped[str] = mapped_column(String, default="unknown")
    __table_args__ = (UniqueConstraint("video_id", "stream_index"),)


class InternalSubtitle(Base):
    __tablename__ = "internal_subtitles"
    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    stream_index: Mapped[int] = mapped_column(Integer)
    codec: Mapped[str | None] = mapped_column(String, nullable=True)
    language: Mapped[str] = mapped_column(String, default="unknown")
    normalized_language: Mapped[str] = mapped_column(String, default="unknown", server_default="unknown")
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (UniqueConstraint("video_id", "stream_index"),)


class ExternalSubtitle(Base):
    __tablename__ = "external_subtitles"
    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    relative_path: Mapped[str] = mapped_column(String)
    codec: Mapped[str] = mapped_column(String)
    language: Mapped[str] = mapped_column(String, default="unknown")
    normalized_language: Mapped[str] = mapped_column(String, default="unknown", server_default="unknown")
    __table_args__ = (UniqueConstraint("video_id", "relative_path"),)
