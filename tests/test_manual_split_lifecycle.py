from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.hierarchy_evaluation import (
    HierarchyIssueCode,
    derive_hierarchy_status,
    evaluate_collection_hierarchy,
)
from app.hierarchy_review import (
    ManualTitleDefinition,
    apply_manual_split,
    hierarchy_review_diagnostics,
    preview_assignments,
    refresh_collection_state,
)
from app.manual_split import (
    ManualSplitDecisionKind,
    evaluate_persisted_manual_split,
)
from app.migrations import migrate_schema
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


def _write_library(root: Path, filenames: tuple[str, ...]) -> None:
    folder = root / "Anime" / "Show"
    folder.mkdir(parents=True)
    for filename in filenames:
        (folder / filename).write_bytes(b"video")


def _definition(
    *,
    title_id: int | None,
    name: str,
    start: int | None,
    end: int | None,
    sort_order: int,
    season: int = 1,
    pattern: str | None = None,
) -> ManualTitleDefinition:
    return ManualTitleDefinition(
        title_id=title_id,
        local_title=name,
        manual_display_title=None,
        season_number_manual=season,
        season_label_manual=f"S{season}",
        part_number_manual=None,
        part_type_manual="season",
        episode_start=start,
        episode_end=end,
        episode_start_offset=None,
        numbering_mode="season_local",
        sort_order=sort_order,
        filename_pattern=pattern,
    )


def _load_collection(session: Session) -> CatalogCollection:
    collections = list(session.scalars(select(CatalogCollection)))
    assert len(collections) == 1
    return collections[0]


def _decision_signature(result) -> tuple[tuple[object, ...], ...]:
    return tuple(sorted(
        (
            decision.video.filename,
            decision.kind.value,
            tuple(rule.index for rule in decision.matching_rules),
        )
        for decision in result.decisions
    ))


def _snapshot(collection: CatalogCollection) -> dict[str, object]:
    videos = list(collection.videos)
    evaluation = evaluate_collection_hierarchy(
        collection,
        videos,
        include_legacy_fallback=False,
    )
    manual_issues = tuple(sorted(
        (
            issue.code.value,
            issue.scope.value,
            tuple(video.filename for video in issue.videos),
            tuple(title.local_title for title in issue.related_catalog_titles),
        )
        for issue in evaluation.issues
        if issue.code in {
            HierarchyIssueCode.MANUAL_SPLIT_CONFLICT,
            HierarchyIssueCode.MANUAL_SPLIT_UNMATCHED,
        }
    ))
    return {
        "assignment": tuple(sorted(
            (
                video.filename,
                video.catalog_title.local_title if video.catalog_title else None,
            )
            for video in videos
        )),
        "status": collection.hierarchy_status,
        "evaluated_status": evaluation.status,
        "manual_issues": manual_issues,
        "all_codes": tuple(sorted(issue.code.value for issue in evaluation.issues)),
        "numbering": tuple(sorted(
            (
                video.filename,
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.media_part_number,
            )
            for video in videos
        )),
    }


def _run_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filenames: tuple[str, ...],
    definitions_factory,
    *,
    configure=None,
    confirm_conflicts: bool = False,
) -> tuple[tuple[dict[str, object], ...], tuple[tuple[object, ...], ...]]:
    library = tmp_path / "library"
    _write_library(library, filenames)
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'manual-split.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, library)
        collection = _load_collection(session)
        if configure is not None:
            configure(collection)
        definitions = definitions_factory(collection.titles[0].id)
        preview = preview_assignments(list(collection.videos), definitions)
        preview_signature = _decision_signature(preview)
        applied = apply_manual_split(
            session,
            collection.id,
            definitions,
            confirm_conflicts=confirm_conflicts,
        )
        assert _decision_signature(applied) == preview_signature
        session.commit()

        scan_library(session, library)
        collection = _load_collection(session)
        fresh_scan = _snapshot(collection)
        refresh_collection_state(collection)
        session.flush()
        runtime_after_scan = _snapshot(collection)
        session.commit()

    migrate_schema(engine)

    with Session(engine) as session:
        collection = _load_collection(session)
        startup = _snapshot(collection)
        refresh_collection_state(collection)
        session.flush()
        runtime_after_startup = _snapshot(collection)

    return (
        fresh_scan,
        runtime_after_scan,
        startup,
        runtime_after_startup,
    ), preview_signature


def test_unique_manual_split_assignment_has_full_lifecycle_parity(
    tmp_path,
    monkeypatch,
):
    snapshots, preview_signature = _run_lifecycle(
        tmp_path,
        monkeypatch,
        ("E01.mkv",),
        lambda title_id: [
            _definition(
                title_id=title_id,
                name="Season 1",
                start=1,
                end=12,
                sort_order=1,
            ),
        ],
        configure=lambda collection: setattr(
            collection.videos[0], "media_part_number", 2,
        ),
    )

    assert preview_signature == (("E01.mkv", "unique", (0,)),)
    assert snapshots.count(snapshots[0]) == len(snapshots)
    snapshot = snapshots[0]
    assert snapshot["assignment"] == (("E01.mkv", "Season 1"),)
    assert snapshot["status"] == snapshot["evaluated_status"] == "verified"
    assert snapshot["manual_issues"] == ()
    assert snapshot["numbering"] == (("E01.mkv", 1, 1, 1, 2),)


def test_manual_split_conflict_has_video_scope_and_full_lifecycle_parity(
    tmp_path,
    monkeypatch,
):
    snapshots, preview_signature = _run_lifecycle(
        tmp_path,
        monkeypatch,
        ("E01.mkv",),
        lambda title_id: [
            _definition(
                title_id=title_id,
                name="Season 1",
                start=1,
                end=12,
                sort_order=1,
            ),
            _definition(
                title_id=None,
                name="Season 2",
                start=1,
                end=12,
                sort_order=2,
                season=2,
            ),
        ],
        confirm_conflicts=True,
    )

    assert preview_signature == (("E01.mkv", "conflict", (0, 1)),)
    assert snapshots.count(snapshots[0]) == len(snapshots)
    snapshot = snapshots[0]
    assert snapshot["assignment"] == (("E01.mkv", None),)
    assert snapshot["status"] == snapshot["evaluated_status"] == "conflict"
    assert snapshot["manual_issues"] == (
        (
            "manual_split_conflict",
            "video",
            ("E01.mkv",),
            ("Season 1", "Season 2"),
        ),
    )
    assert "legacy_unlocalized_review_state" not in snapshot["all_codes"]


def test_manual_split_unmatched_has_video_scope_and_full_lifecycle_parity(
    tmp_path,
    monkeypatch,
):
    snapshots, preview_signature = _run_lifecycle(
        tmp_path,
        monkeypatch,
        ("E01.mkv",),
        lambda title_id: [
            _definition(
                title_id=title_id,
                name="Season 1",
                start=2,
                end=12,
                sort_order=1,
            ),
        ],
    )

    assert preview_signature == (("E01.mkv", "unmatched", ()),)
    assert snapshots.count(snapshots[0]) == len(snapshots)
    snapshot = snapshots[0]
    assert snapshot["assignment"] == (("E01.mkv", None),)
    assert snapshot["status"] == snapshot["evaluated_status"] == "review_required"
    assert snapshot["manual_issues"] == (
        ("manual_split_unmatched", "video", ("E01.mkv",), ()),
    )
    assert "legacy_unlocalized_review_state" not in snapshot["all_codes"]


def test_startup_clears_first_match_assignment_for_conflicting_rules(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'startup-conflict.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
            hierarchy_status="conflict",
        )
        first = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/.catalog-part-1",
            part_type_manual="season",
            season_number_manual=1,
            hierarchy_manual_override=True,
            episode_start=1,
            episode_end=12,
            sort_order_manual=1,
        )
        CatalogTitle(
            collection=collection,
            local_title="Season 2",
            normalized_local_title="season 2",
            relative_root_path="Anime/Show/.catalog-part-2",
            part_type_manual="season",
            season_number_manual=2,
            hierarchy_manual_override=True,
            episode_start=1,
            episode_end=12,
            sort_order_manual=2,
        )
        video = Video(
            relative_path="Anime/Show/E01.mkv",
            root_folder="Anime",
            filename="E01.mkv",
            size=1,
            mtime_ns=1,
            catalog_collection=collection,
            catalog_title=first,
        )
        session.add(video)
        session.commit()

    migrate_schema(engine)

    with Session(engine) as session:
        video = session.scalar(select(Video))
        collection = _load_collection(session)
        evaluation = evaluate_collection_hierarchy(collection, [video])
        assert video.catalog_title_id is None
        assert collection.hierarchy_status == "conflict"
        assert [
            issue.code for issue in evaluation.issues
            if issue.code == HierarchyIssueCode.MANUAL_SPLIT_CONFLICT
        ] == [HierarchyIssueCode.MANUAL_SPLIT_CONFLICT]


def test_conflict_and_numbering_gap_remain_separate_structured_diagnostics(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "library"
    _write_library(library, ("E01.mkv", "E03.mkv", "E05.mkv"))
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, library)
        collection = _load_collection(session)
        definitions = [
            _definition(
                title_id=collection.titles[0].id,
                name="Season 1",
                start=1,
                end=5,
                sort_order=1,
            ),
            _definition(
                title_id=None,
                name="Conflict target",
                start=5,
                end=5,
                sort_order=2,
                season=2,
            ),
        ]
        apply_manual_split(
            session,
            collection.id,
            definitions,
            confirm_conflicts=True,
        )
        session.flush()
        evaluation = evaluate_collection_hierarchy(collection, list(collection.videos))
        diagnostics = hierarchy_review_diagnostics(
            collection,
            list(collection.videos),
            evaluation,
        )
        codes = {issue.code for issue in evaluation.issues}
        assert HierarchyIssueCode.MANUAL_SPLIT_CONFLICT in codes
        assert HierarchyIssueCode.NUMBERING_GAP in codes
        assert collection.hierarchy_status == evaluation.status == "conflict"
        conflict_video = next(video for video in collection.videos if video.filename == "E05.mkv")
        assert [issue.code for issue in diagnostics.for_video(conflict_video)] == [
            "manual_split_conflict",
        ]
        assert any(
            issue.code == "numbering_gap"
            for title in collection.titles
            for issue in diagnostics.for_title(title)
        )


def test_supplementary_and_confirmed_secondary_are_not_false_unmatched(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "library"
    _write_library(
        library,
        (
            "E01.mkv",
            "Unknown copy.mkv",
            "Special 01.mkv",
            "NCOP 01.mkv",
            "NCED 01.mkv",
        ),
    )
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'protected-content.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, library)
        collection = _load_collection(session)
        videos = {video.filename: video for video in collection.videos}
        videos["Unknown copy.mkv"].duplicate_status_manual = "confirmed"
        videos["Unknown copy.mkv"].duplicate_of = videos["E01.mkv"]
        definitions = [
            _definition(
                title_id=collection.titles[0].id,
                name="Season 1",
                start=1,
                end=12,
                sort_order=1,
            ),
        ]
        preview = preview_assignments(list(collection.videos), definitions)
        decisions = {item.video.filename: item.kind for item in preview.decisions}
        assert decisions["E01.mkv"] == ManualSplitDecisionKind.UNIQUE
        assert decisions["Unknown copy.mkv"] == ManualSplitDecisionKind.NOT_REQUIRED
        assert all(
            decisions[name] == ManualSplitDecisionKind.NOT_REQUIRED
            for name in ("Special 01.mkv", "NCOP 01.mkv", "NCED 01.mkv")
        )
        apply_manual_split(session, collection.id, definitions)
        session.commit()
        scan_library(session, library)
        collection = _load_collection(session)
        evaluation = evaluate_collection_hierarchy(collection, list(collection.videos))
        assert HierarchyIssueCode.MANUAL_SPLIT_UNMATCHED not in {
            issue.code for issue in evaluation.issues
        }
        assert all(video.catalog_title_id is not None for video in collection.videos)

    migrate_schema(engine)
    with Session(engine) as session:
        collection = _load_collection(session)
        evaluation = evaluate_collection_hierarchy(collection, list(collection.videos))
        assert HierarchyIssueCode.MANUAL_SPLIT_UNMATCHED not in {
            issue.code for issue in evaluation.issues
        }


def test_manual_split_issue_message_is_not_business_identity():
    collection = CatalogCollection(
        id=1,
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
        hierarchy_status="automatic",
    )
    first = CatalogTitle(
        id=1,
        collection=collection,
        local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Show/.catalog-part-1",
        part_type_manual="season",
        season_number_manual=1,
        hierarchy_manual_override=True,
        episode_start=1,
        episode_end=12,
        sort_order_manual=1,
    )
    CatalogTitle(
        id=2,
        collection=collection,
        local_title="Season 2",
        normalized_local_title="season 2",
        relative_root_path="Anime/Show/.catalog-part-2",
        part_type_manual="season",
        season_number_manual=2,
        hierarchy_manual_override=True,
        episode_start=1,
        episode_end=12,
        sort_order_manual=2,
    )
    video = Video(
        id=1,
        relative_path="Anime/Show/E01.mkv",
        root_folder="Anime",
        filename="E01.mkv",
        size=1,
        mtime_ns=1,
        local_episode_number=1,
        catalog_collection=collection,
        catalog_title=first,
    )

    manual_result = evaluate_persisted_manual_split(collection, [video])
    decision = manual_result.decisions[0]
    evaluation = evaluate_collection_hierarchy(collection, [video])
    issue = next(
        item for item in evaluation.issues
        if item.code == HierarchyIssueCode.MANUAL_SPLIT_CONFLICT
    )
    translated = replace(issue, message="Completely different presentation text")

    assert decision.kind == ManualSplitDecisionKind.CONFLICT
    assert decision.video is video
    assert decision.matching_catalog_titles == tuple(collection.titles)
    assert translated.code == issue.code
    assert translated.scope == issue.scope
    assert translated.videos == issue.videos
    assert translated.related_catalog_titles == issue.related_catalog_titles
    assert derive_hierarchy_status(collection, (issue,)) == "conflict"
    assert derive_hierarchy_status(collection, (translated,)) == "conflict"
