from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.hierarchy_evaluation import (
    LONG_FLAT_SEQUENCE_REVIEW_REASON,
    NUMBERING_REVIEW_SUMMARY,
    HierarchyIssueCode,
    derive_hierarchy_status,
    evaluate_collection_hierarchy,
)
from app.hierarchy_provenance import (
    RELATED_NAMED_CHILD_REVIEW_REASON,
    SUPPLEMENTARY_NAMED_CHILD_REVIEW_REASON,
)
from app.hierarchy_rebuild import (
    apply_hierarchy_rebuild_plan,
    build_hierarchy_rebuild_plan,
)
from app.hierarchy_review import refresh_collection_state
from app.migrations import migrate_schema
from app.models import CatalogCollection, CatalogTitle, TitleMetadata, Video, utc_now
from app.numbering import summarize_title_numbering
from app.scanner import scan_library


PROBE_RESULT = {
    "duration": 60.0,
    "video_codec": "h264",
    "width": 1920,
    "height": 1080,
    "audio": [],
    "subtitles": [],
}


def _write_paths(root: Path, relative_paths: list[str]) -> None:
    for relative_path in relative_paths:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")


def _load_collection(session: Session) -> CatalogCollection:
    collections = list(session.scalars(select(CatalogCollection)))
    assert len(collections) == 1
    return collections[0]


def _snapshot(collection: CatalogCollection) -> dict[str, object]:
    videos = list(collection.videos)
    evaluation = evaluate_collection_hierarchy(
        collection,
        videos,
        include_legacy_fallback=False,
    )
    return {
        "stored_status": collection.hierarchy_status,
        "evaluated_status": evaluation.status,
        "blocking": tuple(sorted(
            issue.code.value for issue in evaluation.blocking_issues
        )),
        "soft": tuple(sorted(
            issue.code.value for issue in evaluation.soft_warnings
        )),
        "note": collection.hierarchy_note,
        "structure": tuple(sorted(
            (
                title.relative_root_path,
                title.effective_part_type,
                title.effective_season_number,
                title.effective_part_number,
            )
            for title in collection.titles
        )),
    }


def _pipeline_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_paths: list[str],
) -> tuple[dict[str, object], ...]:
    library = tmp_path / "library"
    _write_paths(library, relative_paths)
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'pipeline.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, library)
        fresh_scan = _snapshot(_load_collection(session))

        collection = _load_collection(session)
        refresh_collection_state(collection)
        session.flush()
        runtime_after_scan = _snapshot(collection)
        session.commit()

    migrate_schema(engine)

    with Session(engine) as session:
        startup = _snapshot(_load_collection(session))
        collection = _load_collection(session)
        refresh_collection_state(collection)
        session.flush()
        runtime_after_startup = _snapshot(collection)

    return fresh_scan, runtime_after_scan, startup, runtime_after_startup


@pytest.mark.parametrize(
    ("relative_paths", "status", "required_blocking", "soft", "note"),
    [
        (
            [f"Anime/Show/E{number:02}.mkv" for number in range(1, 13)],
            "automatic", (), (), None,
        ),
        (
            ["Anime/Show/E01.mkv", "Anime/Show/E03.mkv"],
            "review_required", ("numbering_gap",), (), NUMBERING_REVIEW_SUMMARY,
        ),
        (
            [
                *[f"Anime/Show/E{number:02}.mkv" for number in range(1, 6)],
                "Anime/Show/Episode 05.mkv",
            ],
            "review_required", ("canonical_duplicate",), (),
            NUMBERING_REVIEW_SUMMARY,
        ),
        (
            ["Anime/Show/Unknown.mkv"],
            "review_required", ("unknown_or_missing_numbering",), (),
            NUMBERING_REVIEW_SUMMARY,
        ),
        (
            [f"Anime/Show/E{number:02}.mkv" for number in range(1, 16)],
            "automatic", (), ("soft_long_flat_series",), None,
        ),
        (
            [f"Anime/Show/E{number:02}.mkv" for number in range(1, 26)],
            "review_required", ("long_flat_series",), (),
            LONG_FLAT_SEQUENCE_REVIEW_REASON,
        ),
        (
            [
                f"Anime/Show/Season 2/E{number:02}.mkv"
                for number in range(1, 26)
            ],
            "automatic", (), (), None,
        ),
    ],
    ids=(
        "direct-e1-e12",
        "numbering-gap",
        "canonical-duplicate",
        "unknown-numbering",
        "soft-long-flat",
        "blocking-long-flat",
        "explicit-season-two",
    ),
)
def test_scanner_startup_and_runtime_share_final_evaluation(
    tmp_path,
    monkeypatch,
    relative_paths,
    status,
    required_blocking,
    soft,
    note,
):
    snapshots = _pipeline_snapshots(tmp_path, monkeypatch, relative_paths)

    assert snapshots.count(snapshots[0]) == len(snapshots)
    snapshot = snapshots[0]
    assert snapshot["stored_status"] == status
    assert snapshot["evaluated_status"] == status
    assert set(required_blocking) <= set(snapshot["blocking"])
    assert snapshot["soft"] == soft
    assert snapshot["note"] == note


@pytest.mark.parametrize(
    ("relative_paths", "issue_code", "note"),
    [
        (
            [
                "Anime/High School DxD/High School DxD Born (J15)/E01.mkv",
            ],
            HierarchyIssueCode.RELATED_NAMED_CHILD,
            RELATED_NAMED_CHILD_REVIEW_REASON,
        ),
        (
            [
                "Anime/High School DxD/NC/High School DxD New/ED 02.mkv",
                "Anime/High School DxD/NC/High School DxD Born/OP 02.mkv",
            ],
            HierarchyIssueCode.SUPPLEMENTARY_NAMED_CHILD,
            SUPPLEMENTARY_NAMED_CHILD_REVIEW_REASON,
        ),
    ],
    ids=("related-named-child", "supplementary-named-child"),
)
def test_named_child_provenance_survives_full_lifecycle(
    tmp_path,
    monkeypatch,
    relative_paths,
    issue_code,
    note,
):
    snapshots = _pipeline_snapshots(tmp_path, monkeypatch, relative_paths)

    assert snapshots.count(snapshots[0]) == len(snapshots)
    snapshot = snapshots[0]
    assert snapshot["stored_status"] == "review_required"
    assert issue_code.value in snapshot["blocking"]
    assert snapshot["note"] == note

    # Presentation text is not business identity or scope.
    engine = create_engine(f"sqlite:///{tmp_path / 'pipeline.db'}")
    with Session(engine) as session:
        collection = _load_collection(session)
        evaluation = evaluate_collection_hierarchy(collection, list(collection.videos))
        issue = next(item for item in evaluation.issues if item.code == issue_code)
        assert issue.scope.value == "catalog_title"
        assert issue.catalog_title_id is not None
        changed_text = replace(issue, message="Jiný uživatelský text")
        assert changed_text.code == issue.code
        assert changed_text.scope == issue.scope
        assert derive_hierarchy_status(collection, (changed_text,)) == "review_required"


def _without_timezone(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=None) if value is not None else None


def test_scanner_and_startup_preserve_manual_snapshots_and_media_part(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "library"
    _write_paths(library, [
        "Anime/Show/Season 1/E01.mkv",
        "Anime/Show/Season 1/Part 2/E01.mkv",
    ])
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'manual.db'}")
    Base.metadata.create_all(engine)
    timestamp = utc_now()

    with Session(engine) as session:
        scan_library(session, library)
        titles = {
            title.relative_root_path: title
            for title in session.scalars(select(CatalogTitle))
        }
        season = titles["Anime/Show/Season 1"]
        season.part_type_manual = "season"
        season.season_number_manual = 1
        season.season_label_manual = "S1"
        season.sort_order_manual = 1
        season.hierarchy_manual_override = True
        season.hierarchy_verified_at = timestamp

        part = titles["Anime/Show/Season 1/Part 2"]
        part.part_type_manual = "part"
        part.season_number_manual = 1
        part.part_number_manual = 2
        part.season_label_manual = "S1"
        part.sort_order_manual = 1002
        part.hierarchy_manual_override = True
        part.hierarchy_verified_at = timestamp
        part.videos[0].media_part_number = 2
        session.commit()

        scan_library(session, library)
        assert part.part_type_manual == "part"
        assert part.season_number_manual == 1
        assert part.part_number_manual == 2
        assert part.videos[0].media_part_number == 2
        session.commit()

    migrate_schema(engine)

    with Session(engine) as session:
        season = session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Show/Season 1"
        ))
        part = session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Show/Season 1/Part 2"
        ))
        assert season.part_type_manual == "season"
        assert season.season_number_manual == 1
        assert season.hierarchy_manual_override is True
        assert _without_timezone(season.hierarchy_verified_at) == _without_timezone(timestamp)
        assert part.part_type_manual == "part"
        assert part.season_number_manual == 1
        assert part.part_number_manual == 2
        assert part.hierarchy_manual_override is True
        assert _without_timezone(part.hierarchy_verified_at) == _without_timezone(timestamp)
        assert part.videos[0].media_part_number == 2
        assert part.collection.hierarchy_status == "verified"

        # Deliberately preserve an incomplete historical Part snapshot too.
        part.part_number_manual = None
        session.commit()

    migrate_schema(engine)

    with Session(engine) as session:
        part = session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Show/Season 1/Part 2"
        ))
        assert part.part_type_manual == "part"
        assert part.season_number_manual == 1
        assert part.part_number_manual is None
        assert part.hierarchy_manual_override is True
        assert _without_timezone(part.hierarchy_verified_at) == _without_timezone(timestamp)
        assert part.videos[0].media_part_number == 2
        assert part.collection.hierarchy_status == "review_required"


def test_duplicate_semantics_match_after_scan_and_startup(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "library"
    primary_path = "Anime/Show/Show - 05.mkv"
    duplicate_path = "Anime/Show/Show 05.mp4"
    _write_paths(library, [primary_path, duplicate_path])
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'duplicates.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, library)
        videos = list(session.scalars(select(Video).order_by(Video.filename)))
        primary, secondary = videos
        secondary.duplicate_status_manual = "confirmed"
        secondary.duplicate_of = primary
        session.commit()
        scan_library(session, library)
        collection = _load_collection(session)
        evaluation = evaluate_collection_hierarchy(collection, list(collection.videos))
        summary = summarize_title_numbering(collection.videos, collection.titles[0])
        assert summary.standard_total == 1
        assert HierarchyIssueCode.CONFIRMED_DUPLICATE in {
            issue.code for issue in evaluation.blocking_issues
        }

    migrate_schema(engine)

    with Session(engine) as session:
        collection = _load_collection(session)
        evaluation = evaluate_collection_hierarchy(collection, list(collection.videos))
        summary = summarize_title_numbering(collection.videos, collection.titles[0])
        assert summary.standard_total == 1
        assert HierarchyIssueCode.CONFIRMED_DUPLICATE in {
            issue.code for issue in evaluation.blocking_issues
        }

    (library / primary_path).unlink()
    with Session(engine) as session:
        scan_library(session, library, confirm_deletions=True)
        collection = _load_collection(session)
        evaluation = evaluate_collection_hierarchy(collection, list(collection.videos))
        assert HierarchyIssueCode.DUPLICATE_PRIMARY_MISSING in {
            issue.code for issue in evaluation.blocking_issues
        }

    migrate_schema(engine)

    with Session(engine) as session:
        collection = _load_collection(session)
        evaluation = evaluate_collection_hierarchy(collection, list(collection.videos))
        assert collection.hierarchy_status == "review_required"
        assert HierarchyIssueCode.DUPLICATE_PRIMARY_MISSING in {
            issue.code for issue in evaluation.blocking_issues
        }


def _season_part_numbering_snapshot(session: Session) -> dict[str, object]:
    collection = _load_collection(session)
    evaluation = evaluate_collection_hierarchy(
        collection,
        list(collection.videos),
        include_legacy_fallback=False,
    )
    return {
        "collection": (
            collection.relative_root_path,
            collection.hierarchy_status,
            collection.hierarchy_note,
        ),
        "titles": tuple(sorted(
            (
                title.relative_root_path,
                title.effective_part_type,
                title.effective_season_number,
                title.effective_part_number,
            )
            for title in collection.titles
        )),
        "videos": tuple(sorted(
            (
                video.relative_path,
                video.catalog_title.relative_root_path,
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.episode_number_source,
                video.media_part_number,
            )
            for video in collection.videos
        )),
        "issues": tuple(sorted(issue.code.value for issue in evaluation.issues)),
    }


def test_fractional_season_part_numbering_matches_scan_startup_runtime_and_rebuild(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "library"
    relative_paths = [
        "Anime/Show/Season 1/Part 1/S01E01.mkv",
        "Anime/Show/Season 1/Part 1/S01E01.5v2.mkv",
        "Anime/Show/Season 1/Part 1/S01E02.mkv",
        "Anime/Show/Season 1/Part 2/S01E01.mkv",
        "Anime/Show/Season 1/Part 2/S01E02.mkv",
        "Anime/Show/Season 2/Part 1/S02E01.mkv",
        "Anime/Show/Season 2/Part 1/S02E02.mkv",
    ]
    _write_paths(library, relative_paths)
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'season-part-lifecycle.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, library)
        collection = _load_collection(session)
        assert len(collection.titles) == 3
        for title in collection.titles:
            title.metadata_record = TitleMetadata(
                catalog_title_id=title.id,
                display_title=title.local_title,
                episode_count=2,
            )
        refresh_collection_state(collection)
        session.commit()
        fresh_scan = _season_part_numbering_snapshot(session)
        before_startup = build_hierarchy_rebuild_plan(session)
        assert before_startup.summary.logical_changes == 0

    migrate_schema(engine)

    with Session(engine) as session:
        startup = _season_part_numbering_snapshot(session)
        collection = _load_collection(session)
        refresh_collection_state(collection)
        session.flush()
        runtime = _season_part_numbering_snapshot(session)

        first_plan = build_hierarchy_rebuild_plan(session)
        assert first_plan.summary.logical_changes == 0
        assert apply_hierarchy_rebuild_plan(session, first_plan).applied is True
        session.commit()

    with Session(engine) as session:
        second_plan = build_hierarchy_rebuild_plan(session)
        rebuilt = _season_part_numbering_snapshot(session)

    assert second_plan.summary.logical_changes == 0
    assert fresh_scan == startup == runtime == rebuilt
    absolute_by_path = {
        row[0]: row[4] for row in rebuilt["videos"]
    }
    assert absolute_by_path[
        "Anime/Show/Season 1/Part 1/S01E01.mkv"
    ] == 1
    assert absolute_by_path[
        "Anime/Show/Season 1/Part 2/S01E01.mkv"
    ] == 3
    assert absolute_by_path[
        "Anime/Show/Season 2/Part 1/S02E01.mkv"
    ] == 5
    fractional = next(
        row for row in rebuilt["videos"] if row[0].endswith("S01E01.5v2.mkv")
    )
    assert fractional[2:6] == (None, None, None, "fractional")
