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
from app.hierarchy_rebuild import build_hierarchy_rebuild_plan
from app.hierarchy_review import (
    ManualTitleDefinition,
    apply_manual_split,
    hierarchy_review_diagnostics,
    preview_assignments,
    refresh_collection_state,
    set_manual_title_hierarchy,
)
from app.manual_split import (
    ManualSplitDecisionKind,
    evaluate_persisted_manual_split,
    manual_split_titles,
)
from app.migrations import migrate_schema
from app.models import (
    CatalogCollection, CatalogTitle, ManualSplitRuleVideo, Video, utc_now,
)
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
    video_ids: tuple[int, ...] = (),
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
        video_ids=video_ids,
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
        "manual_decisions": _decision_signature(
            evaluate_persisted_manual_split(collection, videos)
        ),
        "authority": tuple(sorted(
            (title.local_title, link.video.filename)
            for title in collection.titles
            for link in title.manual_split_rule_videos
        )),
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


def _run_explicit_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    definitions_factory,
    *,
    confirm_conflicts: bool = False,
) -> tuple[tuple[dict[str, object], ...], tuple[tuple[object, ...], ...]]:
    library = tmp_path / "library"
    _write_library(library, ("E01.mkv",))
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'explicit-manual-split.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, library)
        collection = _load_collection(session)
        assert all(
            not title.manual_split_rule_videos for title in collection.titles
        )
        definitions = definitions_factory(collection, collection.videos[0])
        preview = preview_assignments(list(collection.videos), definitions)
        preview_signature = _decision_signature(preview)
        applied = apply_manual_split(
            session,
            collection.id,
            definitions,
            confirm_conflicts=confirm_conflicts,
        )
        assert _decision_signature(applied) == preview_signature
        session.flush()
        after_apply = _snapshot(collection)
        session.commit()

    with Session(engine) as session:
        collection = _load_collection(session)
        after_reload = _snapshot(collection)
        refresh_collection_state(collection)
        session.flush()
        runtime_after_reload = _snapshot(collection)
        session.commit()

        scan_library(session, library)
        collection = _load_collection(session)
        after_scan = _snapshot(collection)
        refresh_collection_state(collection)
        session.flush()
        runtime_after_scan = _snapshot(collection)
        session.commit()

    migrate_schema(engine)

    with Session(engine) as session:
        collection = _load_collection(session)
        after_startup = _snapshot(collection)
        refresh_collection_state(collection)
        session.flush()
        runtime_after_startup = _snapshot(collection)

    return (
        after_apply,
        after_reload,
        runtime_after_reload,
        after_scan,
        runtime_after_scan,
        after_startup,
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


def test_explicit_unique_authority_survives_apply_reload_scan_startup_and_runtime(
    tmp_path,
    monkeypatch,
):
    def definitions(collection, video):
        video.media_part_number = 2
        return [
            _definition(
                title_id=collection.titles[0].id,
                name="Explicit target",
                start=None,
                end=None,
                sort_order=1,
                video_ids=(video.id,),
            ),
        ]

    snapshots, preview_signature = _run_explicit_lifecycle(
        tmp_path,
        monkeypatch,
        definitions,
    )

    expected_decision = (("E01.mkv", "unique", (0,)),)
    assert preview_signature == expected_decision
    for snapshot in snapshots:
        assert snapshot["assignment"] == (("E01.mkv", "Explicit target"),)
        assert snapshot["manual_decisions"] == expected_decision
        assert snapshot["authority"] == (("Explicit target", "E01.mkv"),)
        assert snapshot["manual_issues"] == ()
        assert snapshot["status"] == snapshot["evaluated_status"] == "verified"
        assert snapshot["numbering"] == (("E01.mkv", 1, 1, 1, 2),)


def test_two_explicit_authorities_keep_conflict_with_null_assignment_across_lifecycle(
    tmp_path,
    monkeypatch,
):
    def definitions(collection, video):
        return [
            _definition(
                title_id=collection.titles[0].id,
                name="Explicit A",
                start=None,
                end=None,
                sort_order=1,
                video_ids=(video.id,),
            ),
            _definition(
                title_id=None,
                name="Explicit B",
                start=None,
                end=None,
                sort_order=2,
                season=2,
                video_ids=(video.id,),
            ),
        ]

    snapshots, preview_signature = _run_explicit_lifecycle(
        tmp_path,
        monkeypatch,
        definitions,
        confirm_conflicts=True,
    )

    expected_decision = (("E01.mkv", "conflict", (0, 1)),)
    expected_issue = (
        (
            "manual_split_conflict",
            "video",
            ("E01.mkv",),
            ("Explicit A", "Explicit B"),
        ),
    )
    expected_authority = (
        ("Explicit A", "E01.mkv"),
        ("Explicit B", "E01.mkv"),
    )
    assert preview_signature == expected_decision
    for snapshot in snapshots:
        assert snapshot["assignment"] == (("E01.mkv", None),)
        assert snapshot["manual_decisions"] == expected_decision
        assert snapshot["authority"] == expected_authority
        assert snapshot["manual_issues"] == expected_issue
        assert snapshot["status"] == snapshot["evaluated_status"] == "conflict"
        assert "legacy_unlocalized_review_state" not in snapshot["all_codes"]


def test_explicit_and_range_authorities_do_not_degrade_to_unique_range_after_reload(
    tmp_path,
    monkeypatch,
):
    def definitions(collection, video):
        return [
            _definition(
                title_id=collection.titles[0].id,
                name="Explicit A",
                start=None,
                end=None,
                sort_order=1,
                video_ids=(video.id,),
            ),
            _definition(
                title_id=None,
                name="Range B",
                start=1,
                end=12,
                sort_order=2,
                season=2,
            ),
        ]

    snapshots, preview_signature = _run_explicit_lifecycle(
        tmp_path,
        monkeypatch,
        definitions,
        confirm_conflicts=True,
    )

    expected_decision = (("E01.mkv", "conflict", (0, 1)),)
    assert preview_signature == expected_decision
    for snapshot in snapshots:
        assert snapshot["assignment"] == (("E01.mkv", None),)
        assert snapshot["manual_decisions"] == expected_decision
        assert snapshot["authority"] == (("Explicit A", "E01.mkv"),)
        assert snapshot["manual_issues"] == (
            (
                "manual_split_conflict",
                "video",
                ("E01.mkv",),
                ("Explicit A", "Range B"),
            ),
        )
        assert snapshot["status"] == snapshot["evaluated_status"] == "conflict"
        assert "legacy_unlocalized_review_state" not in snapshot["all_codes"]


def test_persisted_explicit_authority_overrides_stale_result_assignment():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        titles = [
            CatalogTitle(
                collection=collection,
                local_title=name,
                normalized_local_title=name.casefold(),
                relative_root_path=f"Anime/Show/.catalog-part-{index}",
                hierarchy_manual_override=True,
                part_type_manual="season",
                season_number_manual=index,
                sort_order_manual=index,
            )
            for index, name in enumerate(("Stale A", "Explicit B", "Explicit C"), 1)
        ]
        video = Video(
            relative_path="Anime/Show/E01.mkv",
            root_folder="Anime",
            filename="E01.mkv",
            size=1,
            mtime_ns=1,
            catalog_collection=collection,
            catalog_title=titles[0],
        )
        session.add(video)
        session.flush()
        titles[1].manual_split_rule_videos.append(ManualSplitRuleVideo(video=video))
        session.flush()

        unique = evaluate_persisted_manual_split(collection, [video]).decisions[0]
        assert unique.kind == ManualSplitDecisionKind.UNIQUE
        assert unique.target_catalog_title is titles[1]
        video.catalog_title = titles[0]
        refresh_collection_state(collection)
        assert video.catalog_title is titles[1]
        assert [(link.catalog_title_id, link.video_id) for link in video.manual_split_rule_videos] == [
            (titles[1].id, video.id),
        ]

        titles[2].manual_split_rule_videos.append(ManualSplitRuleVideo(video=video))
        session.flush()
        conflict = evaluate_persisted_manual_split(collection, [video]).decisions[0]
        assert conflict.kind == ManualSplitDecisionKind.CONFLICT
        assert conflict.matching_catalog_titles == (titles[1], titles[2])


def test_legacy_assignment_does_not_hide_reproducible_range_conflict():
    collection = CatalogCollection(
        id=1,
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    stale = CatalogTitle(
        id=1,
        collection=collection,
        local_title="Legacy assignment",
        normalized_local_title="legacy assignment",
        relative_root_path="Anime/Show/.catalog-part-1",
        hierarchy_manual_override=True,
        part_type_manual="season",
        season_number_manual=1,
        sort_order_manual=1,
    )
    ranges = [
        CatalogTitle(
            id=index,
            collection=collection,
            local_title=name,
            normalized_local_title=name.casefold(),
            relative_root_path=f"Anime/Show/.catalog-part-{index}",
            hierarchy_manual_override=True,
            part_type_manual="season",
            season_number_manual=index,
            episode_start=1,
            episode_end=12,
            sort_order_manual=index,
        )
        for index, name in ((2, "Range B"), (3, "Range C"))
    ]
    video = Video(
        id=1,
        relative_path="Anime/Show/E01.mkv",
        root_folder="Anime",
        filename="E01.mkv",
        size=1,
        mtime_ns=1,
        catalog_collection=collection,
        catalog_title=stale,
    )

    decision = evaluate_persisted_manual_split(collection, [video]).decisions[0]

    assert decision.kind == ManualSplitDecisionKind.CONFLICT
    assert decision.matching_catalog_titles == tuple(ranges)
    evaluation = evaluate_collection_hierarchy(
        collection,
        [video],
        include_legacy_fallback=False,
    )
    assert evaluation.status == "conflict"
    assert [
        issue.related_catalog_titles
        for issue in evaluation.issues
        if issue.code == HierarchyIssueCode.MANUAL_SPLIT_CONFLICT
    ] == [tuple(ranges)]


def test_scanner_does_not_create_authority_for_new_video(tmp_path, monkeypatch):
    library = tmp_path / "library"
    _write_library(library, ("E01.mkv",))
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, library)
        collection = _load_collection(session)
        first = collection.videos[0]
        apply_manual_split(
            session,
            collection.id,
            [
                _definition(
                    title_id=collection.titles[0].id,
                    name="Explicit target",
                    start=None,
                    end=None,
                    sort_order=1,
                    video_ids=(first.id,),
                )
            ],
        )
        session.commit()

        (library / "Anime" / "Show" / "E02.mkv").write_bytes(b"video")
        scan_library(session, library)
        collection = _load_collection(session)
        videos = {video.filename: video for video in collection.videos}
        authority = {
            (link.catalog_title.local_title, link.video.filename)
            for title in collection.titles
            for link in title.manual_split_rule_videos
        }

        assert authority == {("Explicit target", "E01.mkv")}
        assert videos["E02.mkv"].catalog_title_id is None
        assert videos["E02.mkv"].manual_split_rule_videos == []


def test_complete_hierarchy_override_without_selectors_keeps_structural_assignment(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "library"
    _write_library(library, ("E01.mkv",))
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        scan_library(session, library)
        collection = _load_collection(session)
        title = collection.titles[0]
        title.part_type_manual = "season"
        title.season_number_manual = 1
        title.part_number_manual = None
        title.season_label_manual = "S1"
        title.sort_order_manual = title.sort_order
        title.hierarchy_manual_override = True
        title.hierarchy_verified_at = utc_now()
        session.commit()
        title_id = title.id

        (library / "Anime" / "Show" / "E02.mkv").write_bytes(b"video")
        scan_library(session, library)
        collection = _load_collection(session)
        new_video = next(
            video for video in collection.videos if video.filename == "E02.mkv"
        )

        assert manual_split_titles(collection) == []
        assert new_video.catalog_title_id == title_id
        assert new_video.catalog_collection_id == collection.id
        assert new_video.manual_split_rule_videos == []
        assert collection.hierarchy_status == "verified"


def test_selector_edit_removes_exact_authority_without_resetting_hierarchy():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show", part_type="season", season_number=1,
        )
        video = Video(
            relative_path="Anime/Show/E01.mkv", root_folder="Anime",
            filename="E01.mkv", size=1, mtime_ns=1,
            catalog_collection=collection, catalog_title=title,
        )
        session.add(video)
        session.commit()

        explicit = _definition(
            title_id=title.id, name="Season 1", start=None, end=None,
            sort_order=1, video_ids=(video.id,),
        )
        apply_manual_split(session, collection.id, [explicit])
        session.flush()
        assert {(link.catalog_title_id, link.video_id) for link in video.manual_split_rule_videos} == {
            (title.id, video.id),
        }

        apply_manual_split(
            session,
            collection.id,
            [replace(explicit, video_ids=())],
        )
        session.flush()

        assert video.manual_split_rule_videos == []
        assert title.hierarchy_manual_override is True
        assert title.part_type_manual == "season"


def test_hierarchy_reset_preserves_independent_selector_authority():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show", part_type="season", season_number=1,
        )
        video = Video(
            relative_path="Anime/Show/E01.mkv", root_folder="Anime",
            filename="E01.mkv", size=1, mtime_ns=1,
            catalog_collection=collection, catalog_title=title,
        )
        session.add(video)
        session.commit()
        apply_manual_split(
            session,
            collection.id,
            [_definition(
                title_id=title.id, name="Season 1", start=None, end=None,
                sort_order=1, video_ids=(video.id,),
            )],
        )

        set_manual_title_hierarchy(
            title,
            season_number=None,
            season_label=None,
            part_type=None,
            sort_order=None,
            hierarchy_verified=False,
        )
        session.flush()

        assert title.hierarchy_manual_override is False
        assert [(link.catalog_title_id, link.video_id) for link in title.manual_split_rule_videos] == [
            (title.id, video.id),
        ]
        assert evaluate_persisted_manual_split(collection).decisions[0].kind == (
            ManualSplitDecisionKind.UNIQUE
        )


def test_manual_write_is_stable_in_following_rebuild_dry_run():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Show",
            normalized_local_title="show", relative_root_path="Anime/Show",
            part_type="season", season_number=1,
        )
        video = Video(
            relative_path="Anime/Show/E01.mkv", root_folder="Anime",
            filename="E01.mkv", size=1, mtime_ns=1,
            catalog_collection=collection, catalog_title=title,
        )
        session.add(video)
        session.commit()
        apply_manual_split(
            session,
            collection.id,
            [_definition(
                title_id=title.id, name="Show", start=None, end=None,
                sort_order=1, video_ids=(video.id,),
            )],
        )
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        assignment = next(
            item for item in plan.video_assignments if item.video_id == video.id
        )

        assert assignment.manual_split_kind == "unique"
        assert assignment.target_collection_path == collection.relative_root_path
        assert assignment.target_title_path == title.relative_root_path
        assert assignment.changed is False
        assert not any(blocker.prevents_apply for blocker in plan.blockers)
        assert [(link.catalog_title_id, link.video_id) for link in title.manual_split_rule_videos] == [
            (title.id, video.id),
        ]


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
        assert all(
            not title.manual_split_rule_videos for title in collection.titles
        )
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
