from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    local_episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    absolute_episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_number_source: Mapped[str] = mapped_column(String, default="unknown", server_default="unknown")
    episode_number_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    episode_number_manual_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_number_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    catalog_title_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_titles.id"), nullable=True, index=True
    )
    catalog_collection_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_collections.id"), nullable=True, index=True
    )

    audio_tracks: Mapped[list[AudioTrack]] = relationship(cascade="all, delete-orphan")
    internal_subtitles: Mapped[list[InternalSubtitle]] = relationship(cascade="all, delete-orphan")
    external_subtitles: Mapped[list[ExternalSubtitle]] = relationship(cascade="all, delete-orphan")
    catalog_title: Mapped[CatalogTitle | None] = relationship(back_populates="videos")
    catalog_collection: Mapped[CatalogCollection | None] = relationship(back_populates="videos")


METADATA_STATUSES = (
    "unlinked", "candidates_available", "linked_auto", "linked_manual",
    "conflict", "migration_review_required", "unavailable", "error",
)
HIERARCHY_STATUSES = (
    "automatic", "review_required", "verified", "conflict", "not_applicable",
)


class CatalogCollection(Base):
    __tablename__ = "catalog_collections"
    id: Mapped[int] = mapped_column(primary_key=True)
    local_title: Mapped[str] = mapped_column(String, nullable=False)
    normalized_local_title: Mapped[str] = mapped_column(String, nullable=False, index=True)
    relative_root_path: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    manual_display_title: Mapped[str | None] = mapped_column(String, nullable=True)
    hierarchy_status: Mapped[str] = mapped_column(
        String, default="automatic", server_default="automatic", index=True
    )
    hierarchy_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hierarchy_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_period_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    titles: Mapped[list[CatalogTitle]] = relationship(back_populates="collection")
    videos: Mapped[list[Video]] = relationship(back_populates="catalog_collection")
    __table_args__ = (CheckConstraint(
        "hierarchy_status IN ('automatic','review_required','verified','conflict','not_applicable')",
        name="ck_catalog_collection_hierarchy_status",
    ),)


class CatalogTitle(Base):
    __tablename__ = "catalog_titles"
    id: Mapped[int] = mapped_column(primary_key=True)
    catalog_collection_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_collections.id"), nullable=True, index=True
    )
    local_title: Mapped[str] = mapped_column(String, nullable=False)
    normalized_local_title: Mapped[str] = mapped_column(String, nullable=False, index=True)
    relative_root_path: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    part_type: Mapped[str] = mapped_column(String, default="title", server_default="title")
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_label: Mapped[str | None] = mapped_column(String, nullable=True)
    original_folder_name: Mapped[str | None] = mapped_column(String, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    part_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    numbering_mode: Mapped[str] = mapped_column(String, default="unknown", server_default="unknown")
    numbering_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    numbering_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hierarchy_manual_override: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    season_number_manual: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_label_manual: Mapped[str | None] = mapped_column(String, nullable=True)
    part_type_manual: Mapped[str | None] = mapped_column(String, nullable=True)
    sort_order_manual: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hierarchy_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    episode_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_filename_pattern: Mapped[str | None] = mapped_column(String, nullable=True)
    manual_display_title: Mapped[str | None] = mapped_column(String, nullable=True)
    preferred_metadata_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    preferred_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_status: Mapped[str] = mapped_column(String, default="unlinked", server_default="unlinked")
    metadata_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    videos: Mapped[list[Video]] = relationship(back_populates="catalog_title")
    collection: Mapped[CatalogCollection | None] = relationship(back_populates="titles")
    external_links: Mapped[list[ExternalTitleLink]] = relationship(cascade="all, delete-orphan")
    metadata_record: Mapped[TitleMetadata | None] = relationship(cascade="all, delete-orphan")
    __table_args__ = (CheckConstraint(
        "metadata_status IN ('unlinked','candidates_available','linked_auto','linked_manual','conflict','migration_review_required','unavailable','error')",
        name="ck_catalog_title_metadata_status",
    ),)

    @property
    def effective_season_number(self) -> int | None:
        return self.season_number_manual if self.season_number_manual is not None else self.season_number

    @property
    def effective_season_label(self) -> str | None:
        return self.season_label_manual or self.season_label

    @property
    def effective_part_type(self) -> str:
        return self.part_type_manual or self.part_type

    @property
    def effective_sort_order(self) -> int:
        if self.sort_order_manual is not None:
            return self.sort_order_manual
        if self.season_number_manual is not None:
            return self.season_number_manual
        return self.sort_order


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    __table_args__ = (
        UniqueConstraint("catalog_title_id", "provider", "external_id"),
        Index(
            "ux_external_title_primary", "catalog_title_id", unique=True,
            sqlite_where=(is_primary == True),  # noqa: E712
        ),
    )


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
    cover_image_url: Mapped[str | None] = mapped_column(String, nullable=True)
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
