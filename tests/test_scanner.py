from pathlib import Path
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.catalog import set_manual_hardsub
from app.hierarchy_review import (
    MISSING_DUPLICATE_PRIMARY_REVIEW_REASON, confirm_duplicate_videos,
)
from app.models import CatalogCollection, CatalogTitle, ExternalSubtitle, Video
from app.scanner import LibrarySafetyError, iter_videos, scan_library


PROBE_RESULT = {
    "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
    "audio": [], "subtitles": [],
}


def test_root_videos_remain_visible_unassigned_and_are_not_merged(tmp_path: Path, monkeypatch):
    paths = [tmp_path / "Movie One.mkv", tmp_path / "Movie Two.ova.mp4"]
    for path in paths:
        path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        result = scan_library(session, tmp_path)
        videos = session.scalars(select(Video).order_by(Video.filename)).all()

        assert result.created == 2
        assert [video.relative_path for video in videos] == [
            "Movie One.mkv", "Movie Two.ova.mp4",
        ]
        assert {video.root_folder for video in videos} == {"."}
        assert [video.file_type for video in videos] == ["other", "ova"]
        assert all(video.catalog_collection_id is None for video in videos)
        assert all(video.catalog_title_id is None for video in videos)
        assert session.scalar(select(func.count()).select_from(CatalogCollection)) == 0
        assert all(path.exists() for path in paths)


def test_scan_preserves_meaningful_root_assignment_and_regular_folder_hierarchy(
    tmp_path: Path, monkeypatch
):
    root_path = tmp_path / "Standalone Movie.mkv"
    collection_only_path = tmp_path / "Unassigned Special.mkv"
    regular_path = tmp_path / "Regular Show" / "E01.mkv"
    regular_path.parent.mkdir()
    root_path.write_bytes(b"root")
    collection_only_path.write_bytes(b"root without title")
    regular_path.write_bytes(b"regular")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        root_video = session.scalar(select(Video).where(Video.relative_path == root_path.name))
        collection_only_video = session.scalar(select(Video).where(
            Video.relative_path == collection_only_path.name
        ))
        collection = CatalogCollection(
            local_title="Standalone Movie", normalized_local_title="standalone movie",
            relative_root_path="@root/manual-movie", hierarchy_status="review_required",
        )
        title = CatalogTitle(
            collection=collection, local_title="Standalone Movie",
            normalized_local_title="standalone movie",
            relative_root_path="@root/manual-movie/title", part_type="film",
            hierarchy_manual_override=True,
        )
        root_video.catalog_collection = collection
        root_video.catalog_title = title
        holding_collection = CatalogCollection(
            local_title="Existing holding collection",
            normalized_local_title="existing holding collection",
            relative_root_path="Anime/Existing holding collection",
            hierarchy_status="review_required",
        )
        collection_only_video.catalog_collection = holding_collection
        session.commit()

        scan_library(session, tmp_path)
        stored_root = session.scalar(select(Video).where(Video.relative_path == root_path.name))
        stored_collection_only = session.scalar(select(Video).where(
            Video.relative_path == collection_only_path.name
        ))
        regular = session.scalar(select(Video).where(Video.relative_path == "Regular Show/E01.mkv"))

        assert stored_root.catalog_collection_id == collection.id
        assert stored_root.catalog_title_id == title.id
        assert stored_collection_only.catalog_collection_id == holding_collection.id
        assert stored_collection_only.catalog_title_id is None
        assert regular.catalog_collection.local_title == "Regular Show"
        assert regular.catalog_title.local_title == "Regular Show"
        assert root_path.exists() and collection_only_path.exists() and regular_path.exists()


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
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: {
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


def test_scan_preserves_manual_episode_override(tmp_path: Path, monkeypatch):
    video_path = tmp_path / "Show" / "Part 2" / "Episode 14.mkv"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        scan_library(session, tmp_path)
        video = session.scalar(select(Video))
        video.episode_number_manual_override = 20
        session.commit()
        scan_library(session, tmp_path)
        video = session.scalar(select(Video))
        assert video.episode_number_manual_override == 20
        assert video.episode_number_source == "manual"


def test_scan_preserves_manual_hierarchy_values(tmp_path: Path, monkeypatch):
    video_path = tmp_path / "OVERLORD" / "Overlord (L15)" / "Episode 01.mkv"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        scan_library(session, tmp_path)
        title = session.scalar(select(CatalogTitle))
        title.season_number_manual = 1
        title.season_label_manual = "S1"
        title.part_type_manual = "season"
        title.sort_order_manual = 10
        title.hierarchy_manual_override = True
        session.commit()

        scan_library(session, tmp_path)
        title = session.scalar(select(CatalogTitle))
        assert title.season_number_manual == 1
        assert title.season_label_manual == "S1"
        assert title.part_type_manual == "season"
        assert title.sort_order_manual == 10


def test_updates_language_of_existing_external_subtitle(tmp_path: Path, monkeypatch):
    video_path = tmp_path / "Show" / "episode.mkv"
    video_path.parent.mkdir()
    video_path.write_bytes(b"video")
    subtitle_path = video_path.parent / "episode.srt"
    subtitle_path.write_text("subtitle", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: {
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
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: {
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
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
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
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
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
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
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
        absence_timestamp = video.manual_hardsub_verified_at
        assert absence_timestamp is not None

        scan_library(session, tmp_path)
        video = session.scalar(select(Video))
        assert video.manual_hardsub_cs is False
        assert video.manual_hardsub_sk is False
        assert video.manual_hardsub_verified_at == absence_timestamp

        set_manual_hardsub(video, "unknown")
        session.commit()
        assert video.manual_hardsub_verified_at is None


def test_scan_preserves_duplicate_relationship_and_marks_missing_primary(
    tmp_path: Path, monkeypatch,
):
    folder = tmp_path / "Show"
    folder.mkdir()
    primary_path = folder / "Show - 01.mkv"
    duplicate_path = folder / "Show 01.mp4"
    primary_path.write_bytes(b"primary")
    duplicate_path.write_bytes(b"duplicate")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        videos = list(session.scalars(select(Video).order_by(Video.filename)))
        collection = videos[0].catalog_collection
        primary = next(video for video in videos if video.filename == primary_path.name)
        duplicate = next(video for video in videos if video.filename == duplicate_path.name)
        confirm_duplicate_videos(
            session, collection.id, [primary.id, duplicate.id], primary.id,
        )
        session.commit()
        primary_id, duplicate_id, collection_id = primary.id, duplicate.id, collection.id

        scan_library(session, tmp_path)
        duplicate = session.get(Video, duplicate_id)
        assert duplicate.duplicate_of_video_id == primary_id

        monkeypatch.setattr(
            "app.scanner.service.iter_videos", lambda _: iter([duplicate_path]),
        )
        scan_library(session, tmp_path, confirm_deletions=True)

    with sessions() as session:
        assert session.get(Video, primary_id) is None
        duplicate = session.get(Video, duplicate_id)
        collection = session.get(CatalogCollection, collection_id)
        assert duplicate is not None
        assert duplicate.duplicate_of_video_id is None
        assert duplicate.duplicate_primary_missing is True
        assert collection.hierarchy_status == "review_required"
        assert collection.hierarchy_note == MISSING_DUPLICATE_PRIMARY_REVIEW_REASON
        assert primary_path.read_bytes() == b"primary"
        assert duplicate_path.read_bytes() == b"duplicate"
