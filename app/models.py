from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint
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

    audio_tracks: Mapped[list[AudioTrack]] = relationship(cascade="all, delete-orphan")
    internal_subtitles: Mapped[list[InternalSubtitle]] = relationship(cascade="all, delete-orphan")
    external_subtitles: Mapped[list[ExternalSubtitle]] = relationship(cascade="all, delete-orphan")


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
