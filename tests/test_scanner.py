from pathlib import Path
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.catalog import set_manual_hardsub
from app.models import ExternalSubtitle, Video
from app.scanner import LibrarySafetyError, iter_videos, scan_library


PROBE_RESULT = {
    "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
    "audio": [], "subtitles": [],
}


def test_ignores_recycle(tmp_path: Path):
    (tmp_path / "#recycle").mkdir()
    (tmp_path / "#recycle" / "deleted.mkv").touch()
    (tmp_path / "Anime").mkdir()
    (tmp_path / "Anime" / "episode.mkv").touch()
    assert [path.name for path in iter_videos(tmp_path)] == ["episode.mkv"]


def test_repeated_scan_has_no_duplicates(tmp_path: Path, monkeypatch):
    video_path = tmp_path / "Show" / "episode.mkv"
    video_path.parent.mkdir()
    video_path.write_bytes(b"video")
    (video_path.parent / "episode.cs.srt").write_text("Jsem tady, protože něco vím.", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _: {
        "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
        "audio": [], "subtitles": [],
    })
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        first = scan_library(session, tmp_path)
        second = scan_library(session, tmp_path)
        assert first.created == 1
        assert second.unchanged == 1
        assert session.scalar(select(func.count()).select_from(Video)) == 1
        assert session.scalar(select(func.count()).select_from(ExternalSubtitle)) == 1


def test_updates_language_of_existing_external_subtitle(tmp_path: Path, monkeypatch):
    video_path = tmp_path / "Show" / "episode.mkv"
    video_path.parent.mkdir()
    video_path.write_bytes(b"video")
    subtitle_path = video_path.parent / "episode.srt"
    subtitle_path.write_text("subtitle", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _: {
        "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
        "audio": [], "subtitles": [],
    })
    detected_language = "cs"
    monkeypatch.setattr("app.scanner.service.read_and_detect", lambda _: detected_language)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        subtitle_id = session.scalar(select(ExternalSubtitle.id))
        assert session.scalar(select(ExternalSubtitle.language)) == "cs"

        detected_language = "sk"
        scan_library(session, tmp_path)

        subtitle = session.scalar(select(ExternalSubtitle))
        assert subtitle.id == subtitle_id
        assert subtitle.language == "sk"
        assert session.scalar(select(func.count()).select_from(ExternalSubtitle)) == 1


def test_preserves_two_external_subtitles_with_same_language(tmp_path: Path, monkeypatch):
    video_path = tmp_path / "Show" / "episode.mkv"
    video_path.parent.mkdir()
    video_path.write_bytes(b"video")
    (video_path.parent / "episode.cs.srt").write_text("one", encoding="utf-8")
    (video_path.parent / "episode.alternative.srt").write_text("two", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _: {
        "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
        "audio": [], "subtitles": [],
    })
    monkeypatch.setattr("app.scanner.service.read_and_detect", lambda _: "cs")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        subtitles = session.scalars(select(ExternalSubtitle).order_by(ExternalSubtitle.relative_path)).all()
        assert len(subtitles) == 2
        assert {subtitle.normalized_language for subtitle in subtitles} == {"cs"}
        assert len({subtitle.relative_path for subtitle in subtitles}) == 2


def test_empty_existing_root_does_not_delete_database_records(tmp_path: Path, monkeypatch):
    video_path = tmp_path / "Show" / "episode.mkv"
    video_path.parent.mkdir()
    video_path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        video_path.unlink()

        with pytest.raises(LibrarySafetyError, match="může být odpojená"):
            scan_library(session, tmp_path)

        assert session.scalar(select(func.count()).select_from(Video)) == 1


def test_required_mount_stops_scan_before_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.scanner.service.is_on_mounted_source", lambda _: False)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        with pytest.raises(LibrarySafetyError, match="neleží na připojeném zdroji"):
            scan_library(session, tmp_path, require_mount=True)
        assert session.scalar(select(func.count()).select_from(Video)) == 0


def test_removing_more_than_twenty_percent_requires_confirmation(tmp_path: Path, monkeypatch):
    show = tmp_path / "Show"
    show.mkdir()
    video_paths = []
    for number in range(10):
        path = show / f"episode-{number:02}.mkv"
        path.write_bytes(b"video")
        video_paths.append(path)
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        for path in video_paths[:3]:
            path.unlink()

        with pytest.raises(LibrarySafetyError, match="explicitně potvrďte") as error:
            scan_library(session, tmp_path)

        assert error.value.confirmation_allowed
        assert session.scalar(select(func.count()).select_from(Video)) == 10

        result = scan_library(session, tmp_path, confirm_deletions=True)
        assert result.removed == 3
        assert session.scalar(select(func.count()).select_from(Video)) == 7


def test_scan_preserves_manual_hardsub_and_verification_date(tmp_path: Path, monkeypatch):
    video_path = tmp_path / "Show" / "episode.mkv"
    video_path.parent.mkdir()
    video_path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    verified_at = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

    with sessions() as session:
        scan_library(session, tmp_path)
        video = session.scalar(select(Video))
        set_manual_hardsub(video, "both", verified_at=verified_at)
        session.commit()
        stored_timestamp = video.manual_hardsub_verified_at

        scan_library(session, tmp_path)
        video = session.scalar(select(Video))
        assert video.manual_hardsub_cs is True
        assert video.manual_hardsub_sk is True
        assert video.manual_hardsub_verified_at == stored_timestamp

        set_manual_hardsub(video, "none")
        session.commit()
        assert video.manual_hardsub_verified_at is None
