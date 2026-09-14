"""R6 closure: manual subtitle/video candidate rejection must survive the
full scanner lifecycle, not only the single-detour case fixed by 2417e3b.

Human rejection of a specific (subtitle, video) pair is authority.  A later
automatic detour through a *different* safe candidate -- and that detour
candidate later disappearing again -- must never resurrect the originally
rejected video as an automatic match.  These tests reproduce the longer
lifecycle the closure audit found (R6-B through R6-G); the basic single-step
case (R6-A) is already covered by test_scanner.py's pre-existing B6 test.
"""
from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from app.database import Base
from app.external_subtitle_compatibility import (
    CONFIRMED_COMPATIBLE, CONFIRMED_INCOMPATIBLE, confirm_compatible,
    confirm_incompatible, external_subtitle_compatibility_status,
)
from app.migrations import STARTUP_COMPATIBILITY_VERSION, migrate_schema_at_startup
from app.models import ExternalSubtitle, UnresolvedExternalSubtitle, Video
from app.scanner import scan_library
from app.subtitle_review import (
    manually_link_subtitle, reopen_manual_subtitle_link,
    set_subtitle_candidate_rejected,
)


PROBE_RESULT = {
    "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
    "audio": [], "subtitles": [],
}


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def test_r6b_rejection_survives_a_detour_through_a_different_automatic_match(
    tmp_path: Path, monkeypatch,
):
    """R6-B: reject A, B auto-matches, B disappears -> A must stay rejected."""
    show = tmp_path / "Show"
    show.mkdir()
    a_path = show / "Show - 01.mkv"
    x_path = show / "Show - 01.mp4"
    a_path.write_bytes(b"video a")
    x_path.write_bytes(b"video x")
    subtitle_path = show / "Show - 01.cs.ass"
    subtitle_path.write_text("subtitle", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = _engine()

    with Session(engine) as session:
        # Two language-suffix candidates -> ambiguous, stays unresolved.
        scan_library(session, tmp_path)
        unresolved = session.scalar(select(UnresolvedExternalSubtitle))
        assert unresolved is not None
        video_a = session.scalar(select(Video).where(Video.filename == "Show - 01.mkv"))
        set_subtitle_candidate_rejected(unresolved, video_a.id, True)
        session.commit()

        # X disappears and an unrelated exact-match candidate B appears: B is
        # the only safe candidate now, and it was never rejected.
        x_path.unlink()
        b_path = show / "Show - 01.cs.mkv"
        b_path.write_bytes(b"video b")
        scan_library(session, tmp_path, confirm_deletions=True)

        linked = session.scalar(select(ExternalSubtitle))
        assert linked is not None, "B is a legitimate, non-rejected safe candidate"
        video_b = session.scalar(select(Video).where(Video.filename == "Show - 01.cs.mkv"))
        assert [row.video_id for row in linked.compatibilities] == [video_b.id]
        assert session.scalar(
            select(func.count()).select_from(UnresolvedExternalSubtitle)
        ) == 0

        # B disappears; only rejected A remains as a filename candidate.
        b_path.unlink()
        scan_library(session, tmp_path, confirm_deletions=True)

        assert session.scalar(select(func.count()).select_from(ExternalSubtitle)) == 0
        unresolved = session.scalar(select(UnresolvedExternalSubtitle))
        assert unresolved is not None, (
            "subtitle must fall back to unresolved, not silently auto-match "
            "the previously rejected video A"
        )
        assert unresolved.status == "unresolved"

        # Repeated rescans (R6-F) must not resurrect A either.
        scan_library(session, tmp_path)
        scan_library(session, tmp_path)
        assert session.scalar(select(func.count()).select_from(ExternalSubtitle)) == 0
        unresolved = session.scalar(select(UnresolvedExternalSubtitle))
        assert unresolved is not None and unresolved.status == "unresolved"


def test_r6c_rejection_survives_multiple_consecutive_detours(
    tmp_path: Path, monkeypatch,
):
    """R6-C: reject A; auto B, B gone; auto C, C gone -> A still rejected."""
    show = tmp_path / "Show"
    show.mkdir()
    a_path = show / "Show - 01.mkv"
    x_path = show / "Show - 01.mp4"
    a_path.write_bytes(b"video a")
    x_path.write_bytes(b"video x")
    subtitle_path = show / "Show - 01.cs.ass"
    subtitle_path.write_text("subtitle", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = _engine()

    with Session(engine) as session:
        scan_library(session, tmp_path)
        unresolved = session.scalar(select(UnresolvedExternalSubtitle))
        video_a = session.scalar(select(Video).where(Video.filename == "Show - 01.mkv"))
        set_subtitle_candidate_rejected(unresolved, video_a.id, True)
        session.commit()

        x_path.unlink()
        b_path = show / "Show - 01.cs.mkv"
        b_path.write_bytes(b"video b")
        scan_library(session, tmp_path, confirm_deletions=True)
        assert session.scalar(select(func.count()).select_from(ExternalSubtitle)) == 1

        b_path.unlink()
        c_path = show / "Show - 01.cs.avi"
        c_path.write_bytes(b"video c")
        scan_library(session, tmp_path, confirm_deletions=True)
        linked = session.scalar(select(ExternalSubtitle))
        assert linked is not None
        video_c = session.scalar(select(Video).where(Video.filename == "Show - 01.cs.avi"))
        assert [row.video_id for row in linked.compatibilities] == [video_c.id]

        c_path.unlink()
        scan_library(session, tmp_path, confirm_deletions=True)
        assert session.scalar(select(func.count()).select_from(ExternalSubtitle)) == 0
        unresolved = session.scalar(select(UnresolvedExternalSubtitle))
        assert unresolved is not None and unresolved.status == "unresolved"


def test_r6d_rejection_of_one_candidate_never_blocks_a_different_legitimate_match(
    tmp_path: Path, monkeypatch,
):
    """R6-D: rejecting A must not prevent a legitimate automatic match of B."""
    show = tmp_path / "Show"
    show.mkdir()
    a_path = show / "Show - 01.mkv"
    a_path.write_bytes(b"video a")
    subtitle_path = show / "Show - 01.cs.ass"
    subtitle_path.write_text("subtitle", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = _engine()

    with Session(engine) as session:
        x_path = show / "Show - 01.mp4"
        x_path.write_bytes(b"video x")
        scan_library(session, tmp_path)
        unresolved = session.scalar(select(UnresolvedExternalSubtitle))
        video_a = session.scalar(select(Video).where(Video.filename == "Show - 01.mkv"))
        set_subtitle_candidate_rejected(unresolved, video_a.id, True)
        session.commit()

        x_path.unlink()
        b_path = show / "Show - 01.cs.mkv"
        b_path.write_bytes(b"video b")
        scan_library(session, tmp_path, confirm_deletions=True)

        linked = session.scalar(select(ExternalSubtitle))
        video_b = session.scalar(select(Video).where(Video.filename == "Show - 01.cs.mkv"))
        assert linked is not None
        assert [row.video_id for row in linked.compatibilities] == [video_b.id]
        assert external_subtitle_compatibility_status(linked, video_a) is None


def test_r6e_rejection_survives_a_manual_link_and_reopen_detour(
    tmp_path: Path, monkeypatch,
):
    """R6-E: reject A, manually link B, reopen/unlink B -> A still rejected."""
    show = tmp_path / "Show"
    show.mkdir()
    a_path = show / "Show - 01.mkv"
    b_path = show / "Show - 01.mp4"
    a_path.write_bytes(b"video a")
    b_path.write_bytes(b"video b")
    subtitle_path = show / "Show - 01.ass"
    subtitle_path.write_text("subtitle", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = _engine()

    with Session(engine) as session:
        scan_library(session, tmp_path)
        unresolved = session.scalar(select(UnresolvedExternalSubtitle))
        video_a = session.scalar(select(Video).where(Video.filename == "Show - 01.mkv"))
        video_b = session.scalar(select(Video).where(Video.filename == "Show - 01.mp4"))
        set_subtitle_candidate_rejected(unresolved, video_a.id, True)
        session.commit()

        unresolved = session.scalar(select(UnresolvedExternalSubtitle))
        manually_link_subtitle(session, unresolved, video_b)
        session.commit()

        linked = session.scalar(select(ExternalSubtitle))
        assert linked.match_method == "manual"

        reopened = reopen_manual_subtitle_link(session, linked)
        session.commit()
        assert reopened.rejected_video_ids_json == f"[{video_a.id}]"

        # B disappears entirely; only rejected A remains -- must stay unresolved.
        b_path.unlink()
        scan_library(session, tmp_path, confirm_deletions=True)
        assert session.scalar(select(func.count()).select_from(ExternalSubtitle)) == 0
        unresolved = session.scalar(select(UnresolvedExternalSubtitle))
        assert unresolved is not None and unresolved.status == "unresolved"
        assert unresolved.rejected_video_ids_json == f"[{video_a.id}]"


def test_r6g_confirmed_incompatible_workflow_is_unaffected_by_the_rejection_fix(
    tmp_path: Path, monkeypatch,
):
    """R6-G: the pre-existing compatibility workflow keeps its own semantics."""
    show = tmp_path / "Show"
    show.mkdir()
    (show / "Show - 01.mkv").write_bytes(b"video a")
    (show / "Show - 01.ass").write_text("subtitle", encoding="utf-8")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = _engine()

    with Session(engine) as session:
        scan_library(session, tmp_path)
        linked = session.scalar(select(ExternalSubtitle))
        video = session.scalar(select(Video))
        assert linked.match_method == "automatic"

        confirm_incompatible(session, linked, video)
        session.commit()
        assert external_subtitle_compatibility_status(linked, video) == CONFIRMED_INCOMPATIBLE

        # Scanner must never silently overwrite a human incompatible decision.
        scan_library(session, tmp_path)
        linked = session.scalar(select(ExternalSubtitle))
        assert external_subtitle_compatibility_status(linked, video) == CONFIRMED_INCOMPATIBLE

        confirm_compatible(session, linked, video)
        session.commit()
        assert external_subtitle_compatibility_status(linked, video) == CONFIRMED_COMPATIBLE
        scan_library(session, tmp_path)
        linked = session.scalar(select(ExternalSubtitle))
        assert external_subtitle_compatibility_status(linked, video) == CONFIRMED_COMPATIBLE


def test_startup_migration_adds_rejection_column_to_an_existing_v2_database(
    tmp_path: Path,
):
    """The real upgrade path production will take: user_version 2 -> 3."""
    engine = create_engine(f"sqlite:///{tmp_path / 'v2.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        # Simulate a pre-R6 database: same table, but without the new column.
        connection.execute(text(
            "ALTER TABLE external_subtitles DROP COLUMN rejected_video_ids_json"
        ))
        connection.execute(text(
            "INSERT INTO external_subtitles "
            "(relative_path, codec, language, normalized_language, match_method) "
            "VALUES ('Show/Show - 01.cs.ass', 'ass', 'cs', 'cs', 'manual')"
        ))
        connection.execute(text("PRAGMA user_version = 2"))

    assert "rejected_video_ids_json" not in {
        column["name"] for column in inspect(engine).get_columns("external_subtitles")
    }

    assert migrate_schema_at_startup(engine) is True

    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA user_version")) == STARTUP_COMPATIBILITY_VERSION
    columns = {
        column["name"] for column in inspect(engine).get_columns("external_subtitles")
    }
    assert "rejected_video_ids_json" in columns

    with Session(engine) as session:
        subtitle = session.scalar(select(ExternalSubtitle))
        assert subtitle.relative_path == "Show/Show - 01.cs.ass"
        assert subtitle.match_method == "manual"
        assert subtitle.rejected_video_ids_json == "[]"

    # Idempotent: running it again performs no further writes or version bump.
    assert migrate_schema_at_startup(engine) is False
