from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload

from app.database import Base
from app.hierarchy_evaluation import evaluate_collection_hierarchy
from app.hierarchy_rebuild import (
    apply_hierarchy_rebuild_plan,
    build_hierarchy_rebuild_plan,
)
from app.models import CatalogCollection, CatalogTitle, Video
from app.scanner import scan_library


PROBE_RESULT = {
    "duration": 60.0,
    "video_codec": "h264",
    "width": 1920,
    "height": 1080,
    "audio": [],
    "subtitles": [],
}


@dataclass(frozen=True)
class ParityCase:
    relative_paths: tuple[str, ...]
    expected_status: str
    expected_issue_codes: frozenset[str]
    expected_title_axes: frozenset[tuple[str, str, int | None, int | None]]
    expected_season_numbers: tuple[tuple[str, int | None], ...]


def _write_paths(root: Path, relative_paths: tuple[str, ...]) -> None:
    for relative_path in relative_paths:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")


def _scan_database(engine, library: Path) -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, library)


def _make_hierarchy_stale(engine) -> None:
    """Replace valid membership with disposable automatic stale objects."""
    with Session(engine) as session:
        physical_collections = list(session.scalars(
            select(CatalogCollection).order_by(CatalogCollection.relative_root_path)
        ))
        physical_titles = list(session.scalars(
            select(CatalogTitle).order_by(CatalogTitle.relative_root_path)
        ))
        videos = list(session.scalars(select(Video).order_by(Video.relative_path)))
        assert physical_collections and physical_titles and videos

        stale_collection = CatalogCollection(
            local_title="Obsolete hierarchy fragment",
            normalized_local_title="obsolete hierarchy fragment",
            relative_root_path="Anime/__obsolete_rebuild_fragment__",
        )
        stale_title = CatalogTitle(
            collection=stale_collection,
            local_title="Obsolete Season 99",
            normalized_local_title="obsolete season 99",
            relative_root_path="Anime/__obsolete_rebuild_fragment__/Season 99",
            part_type="season",
            season_number=99,
            season_label="S99",
            sort_order=99,
        )
        session.add(stale_title)
        session.flush()

        for video in videos:
            video.catalog_collection = stale_collection
            video.catalog_title = stale_title
            video.local_episode_number = 999
            video.season_episode_number = 999
            video.absolute_episode_number = 999
            video.external_episode_number = 999
            video.episode_number_source = "stale"
            video.episode_number_confidence = 0.01
        session.flush()

        # Rebuild must both update an existing automatic identity and recreate a
        # missing one. The removed title carries no manual or metadata authority.
        removed_title = physical_titles[-1]
        session.delete(removed_title)
        for title in physical_titles[:-1]:
            title.local_title = f"Stale {title.local_title}"
            title.normalized_local_title = "stale"
            title.part_type = "bonus"
            title.season_number = 88
            title.part_number = 77
            title.season_label = "S88"
            title.original_folder_name = "stale"
            title.sort_order = 8877

        for collection in physical_collections:
            collection.local_title = f"Stale {collection.local_title}"
            collection.normalized_local_title = "stale"
            collection.local_period_hint = "P99"
            collection.hierarchy_status = "review_required"
            collection.hierarchy_note = "Stale hierarchy state"
        session.commit()


def _logical_state(session: Session) -> dict[str, tuple[object, ...]]:
    collections = list(session.scalars(
        select(CatalogCollection).options(
            selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
            selectinload(CatalogCollection.titles).selectinload(
                CatalogTitle.metadata_record
            ),
            selectinload(CatalogCollection.videos).selectinload(Video.catalog_title),
        ).order_by(CatalogCollection.relative_root_path)
    ))
    videos = list(session.scalars(
        select(Video).options(
            selectinload(Video.catalog_collection),
            selectinload(Video.catalog_title).selectinload(CatalogTitle.collection),
        ).order_by(Video.relative_path)
    ))

    issues: list[tuple[object, ...]] = []
    for collection in collections:
        evaluation = evaluate_collection_hierarchy(
            collection,
            list(collection.videos),
            include_legacy_fallback=False,
        )
        issues.extend(
            (
                collection.relative_root_path,
                issue.code.value,
                issue.blocking,
                issue.scope.value,
                (
                    issue.catalog_title.relative_root_path
                    if issue.catalog_title is not None else None
                ),
                tuple(sorted(video.relative_path for video in issue.videos)),
                tuple(sorted(
                    title.relative_root_path
                    for title in issue.related_catalog_titles
                )),
            )
            for issue in evaluation.issues
        )

    return {
        "collections": tuple(
            (
                collection.relative_root_path,
                collection.local_title,
                collection.normalized_local_title,
                collection.local_period_hint,
                collection.hierarchy_status,
                collection.hierarchy_note,
                collection.hierarchy_verified_at is not None,
            )
            for collection in collections
        ),
        "titles": tuple(sorted(
            (
                title.relative_root_path,
                (
                    title.collection.relative_root_path
                    if title.collection is not None else None
                ),
                title.local_title,
                title.normalized_local_title,
                title.part_type,
                title.season_number,
                title.part_number,
                title.season_label,
                title.original_folder_name,
                title.sort_order,
                title.effective_part_type,
                title.effective_season_number,
                title.effective_part_number,
                title.hierarchy_manual_override,
            )
            for collection in collections
            for title in collection.titles
        )),
        "videos": tuple(
            (
                video.relative_path,
                (
                    video.catalog_collection.relative_root_path
                    if video.catalog_collection is not None else None
                ),
                (
                    video.catalog_title.relative_root_path
                    if video.catalog_title is not None else None
                ),
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.external_episode_number,
                video.episode_number_source,
                video.episode_number_confidence,
                video.content_type_manual,
                video.media_part_number,
                video.duplicate_status_manual,
                video.duplicate_primary_missing,
            )
            for video in videos
        ),
        "issues": tuple(sorted(issues)),
    }


CASES = (
    ParityCase(
        relative_paths=tuple(
            f"Anime/Show/E{number:02}.mkv" for number in range(1, 13)
        ),
        expected_status="automatic",
        expected_issue_codes=frozenset(),
        expected_title_axes=frozenset({
            ("Anime/Show", "season", 1, None),
        }),
        expected_season_numbers=tuple(
            (f"Anime/Show/E{number:02}.mkv", number)
            for number in range(1, 13)
        ),
    ),
    ParityCase(
        relative_paths=tuple(
            f"Anime/Show/Season 1/Part {part}/E{number:02}.mkv"
            for part in (1, 2)
            for number in range(1, 4)
        ),
        expected_status="automatic",
        expected_issue_codes=frozenset(),
        expected_title_axes=frozenset({
            ("Anime/Show/Season 1/Part 1", "part", 1, 1),
            ("Anime/Show/Season 1/Part 2", "part", 1, 2),
        }),
        expected_season_numbers=tuple(
            (
                f"Anime/Show/Season 1/Part {part}/E{number:02}.mkv",
                number,
            )
            for part in (1, 2)
            for number in range(1, 4)
        ),
    ),
    ParityCase(
        relative_paths=("Anime/Show/Show Arc/E01.mkv",),
        expected_status="review_required",
        expected_issue_codes=frozenset({
            "generic_structural_type",
            "related_named_child",
        }),
        expected_title_axes=frozenset({
            ("Anime/Show/Show Arc", "title", None, None),
        }),
        expected_season_numbers=(("Anime/Show/Show Arc/E01.mkv", 1),),
    ),
    ParityCase(
        relative_paths=(
            "Anime/Show/NC/Show First/OP 01.mkv",
            "Anime/Show/NC/Show Second/ED 02.mkv",
        ),
        expected_status="review_required",
        expected_issue_codes=frozenset({"supplementary_named_child"}),
        expected_title_axes=frozenset({
            ("Anime/Show/NC/Show First", "bonus", None, None),
            ("Anime/Show/NC/Show Second", "bonus", None, None),
        }),
        expected_season_numbers=(
            ("Anime/Show/NC/Show First/OP 01.mkv", None),
            ("Anime/Show/NC/Show Second/ED 02.mkv", None),
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=("direct-root", "season-and-part", "related-child", "supplementary-child"),
)
def test_fresh_scan_and_stale_hierarchy_rebuild_have_logical_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: ParityCase,
) -> None:
    library = tmp_path / "library"
    _write_paths(library, case.relative_paths)
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    fresh_engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    rebuild_engine = create_engine(f"sqlite:///{tmp_path / 'rebuild.db'}")
    _scan_database(fresh_engine, library)
    _scan_database(rebuild_engine, library)

    with Session(fresh_engine) as session:
        fresh_state = _logical_state(session)

    _make_hierarchy_stale(rebuild_engine)
    with Session(rebuild_engine) as session:
        plan = build_hierarchy_rebuild_plan(session)
        assert plan.has_changes
        assert plan.summary.video_assignments_changed == len(case.relative_paths)
        assert plan.summary.titles_created >= 1
        assert plan.summary.titles_removed >= 1
        assert plan.summary.collections_removed >= 1

        result = apply_hierarchy_rebuild_plan(session, plan)
        assert result.applied is True
        session.commit()

    with Session(rebuild_engine) as session:
        rebuilt_state = _logical_state(session)

    assert rebuilt_state == fresh_state
    assert {
        row[4] for row in rebuilt_state["collections"]
    } == {case.expected_status}
    assert {
        row[1] for row in rebuilt_state["issues"]
    } == case.expected_issue_codes
    assert {
        (row[0], row[10], row[11], row[12])
        for row in rebuilt_state["titles"]
    } == case.expected_title_axes
    assert tuple(
        (row[0], row[4]) for row in rebuilt_state["videos"]
    ) == case.expected_season_numbers

