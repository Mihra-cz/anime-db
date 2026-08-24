from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.catalog import normalize_title
from app.database import Base
from app.hierarchy_evaluation import finalize_collection_hierarchy
from app.hierarchy_rebuild import (
    HierarchyPlanBlockedError,
    ReconciliationAction,
    ReconciliationReason,
    apply_hierarchy_rebuild_plan,
    build_hierarchy_rebuild_plan,
    rebuild_hierarchy,
)
from app.migrations import migrate_schema
from app.hierarchy_review import extract_local_period_hint
from app.models import (
    CatalogCollection,
    CatalogTitle,
    ManualSplitRuleVideo,
    TitleMetadata,
    Video,
    utc_now,
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


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _video(
    relative_path: str,
    *,
    title: CatalogTitle | None = None,
    collection: CatalogCollection | None = None,
    mtime_ns: int = 1,
    **values,
) -> Video:
    return Video(
        relative_path=relative_path,
        root_folder=relative_path.split("/", 1)[0],
        filename=PurePosixPath(relative_path).name,
        size=1,
        mtime_ns=mtime_ns,
        catalog_title=title,
        catalog_collection=collection,
        **values,
    )


def _collection(path: str, title: str | None = None, **values) -> CatalogCollection:
    local_title = title or PurePosixPath(path).name
    return CatalogCollection(
        relative_root_path=path,
        local_title=local_title,
        normalized_local_title=local_title.casefold(),
        **values,
    )


def _title(
    path: str,
    collection: CatalogCollection,
    title: str | None = None,
    **values,
) -> CatalogTitle:
    local_title = title or PurePosixPath(path).name
    return CatalogTitle(
        relative_root_path=path,
        local_title=local_title,
        normalized_local_title=local_title.casefold(),
        collection=collection,
        **values,
    )


def _manual_title(
    collection: CatalogCollection,
    name: str,
    number: int,
    **values,
) -> CatalogTitle:
    return _title(
        f"{collection.relative_root_path}/.manual-{name.casefold()}",
        collection,
        name,
        part_type="season",
        season_number=number,
        season_label=f"S{number}",
        sort_order=number,
        hierarchy_manual_override=True,
        part_type_manual="season",
        season_number_manual=number,
        season_label_manual=f"S{number}",
        sort_order_manual=number,
        hierarchy_verified_at=utc_now(),
        **values,
    )


def _without_timezone(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=None) if value is not None else None


def _database_state(session: Session) -> tuple[object, ...]:
    collections = tuple(
        (
            item.id,
            item.relative_root_path,
            item.local_title,
            item.normalized_local_title,
            item.manual_display_title,
            item.hierarchy_status,
            item.hierarchy_note,
            _without_timezone(item.hierarchy_verified_at),
            item.local_period_hint,
            _without_timezone(item.created_at),
            _without_timezone(item.updated_at),
        )
        for item in session.scalars(
            select(CatalogCollection).order_by(CatalogCollection.relative_root_path)
        )
    )
    titles = tuple(
        (
            item.id,
            item.relative_root_path,
            item.catalog_collection_id,
            item.local_title,
            item.normalized_local_title,
            item.part_type,
            item.season_number,
            item.part_number,
            item.season_label,
            item.original_folder_name,
            item.sort_order,
            item.hierarchy_manual_override,
            item.part_type_manual,
            item.season_number_manual,
            item.part_number_manual,
            item.season_label_manual,
            item.sort_order_manual,
            _without_timezone(item.hierarchy_verified_at),
            item.manual_display_title,
            item.metadata_status,
            _without_timezone(item.created_at),
            _without_timezone(item.updated_at),
        )
        for item in session.scalars(
            select(CatalogTitle).order_by(CatalogTitle.relative_root_path)
        )
    )
    videos = tuple(
        (
            item.id,
            item.relative_path,
            item.catalog_collection_id,
            item.catalog_title_id,
            item.local_episode_number,
            item.season_episode_number,
            item.absolute_episode_number,
            item.external_episode_number,
            item.episode_number_source,
            item.episode_number_confidence,
            item.episode_number_manual_override,
            item.content_type_manual,
            item.media_part_number,
            item.duplicate_status_manual,
            item.duplicate_of_video_id,
            item.duplicate_primary_missing,
        )
        for item in session.scalars(select(Video).order_by(Video.relative_path))
    )
    authority = tuple(session.execute(
        select(
            ManualSplitRuleVideo.catalog_title_id,
            ManualSplitRuleVideo.video_id,
        ).order_by(
            ManualSplitRuleVideo.catalog_title_id,
            ManualSplitRuleVideo.video_id,
        )
    ))
    metadata = tuple(session.execute(
        select(TitleMetadata.catalog_title_id, TitleMetadata.display_title).order_by(
            TitleMetadata.catalog_title_id
        )
    ))
    return collections, titles, videos, authority, metadata


def _assignment(plan, relative_path: str):
    return next(
        item for item in plan.video_assignments if item.relative_path == relative_path
    )


def _actions(items) -> dict[str, ReconciliationAction]:
    return {item.relative_root_path: item.action for item in items}


def test_plan_creates_missing_collection_title_membership_and_numbering():
    engine = _engine()
    paths = [f"Anime/Show/Show - {number:02}.mkv" for number in range(1, 13)]
    with Session(engine) as session:
        session.add_all([
            _video(path, mtime_ns=number)
            for number, path in enumerate(paths, 1)
        ])
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)

        assert plan.summary.collections_created == 1
        assert plan.summary.titles_created == 1
        assert plan.summary.video_assignments_changed == 12
        assert plan.summary.numbering_changes == 12
        assert _actions(plan.collections)["Anime/Show"] == ReconciliationAction.CREATE
        title_item = next(item for item in plan.titles if item.relative_root_path == "Anime/Show")
        assert title_item.action == ReconciliationAction.CREATE
        assert (
            title_item.desired.part_type,
            title_item.desired.season_number,
            title_item.desired.part_number,
            title_item.desired.season_label,
        ) == ("season", 1, None, "S1")

        result = apply_hierarchy_rebuild_plan(session, plan)
        assert result.plan is plan
        assert result.applied is True
        session.commit()

    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection))
        title = session.scalar(select(CatalogTitle))
        videos = list(session.scalars(select(Video).order_by(Video.relative_path)))
        assert collection.relative_root_path == "Anime/Show"
        assert title.collection is collection
        assert (title.part_type, title.season_number, title.part_number) == (
            "season", 1, None,
        )
        assert [video.season_episode_number for video in videos] == list(range(1, 13))
        assert all(video.catalog_title is title for video in videos)
        assert all(video.catalog_collection is collection for video in videos)


def test_plan_creates_one_collection_with_independent_season_and_part_titles():
    engine = _engine()
    paths = [
        "Anime/Show/Season 1/Part 1/E01.mkv",
        "Anime/Show/Season 1/Part 1/E02.mkv",
        "Anime/Show/Season 1/Part 2/E01.mkv",
        "Anime/Show/Season 1/Part 2/E02.mkv",
    ]
    with Session(engine) as session:
        session.add_all([_video(path, mtime_ns=index) for index, path in enumerate(paths, 1)])
        session.commit()
        plan = build_hierarchy_rebuild_plan(session)

        assert plan.summary.collections_created == 1
        assert plan.summary.titles_created == 2
        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        collections = list(session.scalars(select(CatalogCollection)))
        titles = list(session.scalars(select(CatalogTitle).order_by(CatalogTitle.part_number)))
        assert [item.relative_root_path for item in collections] == ["Anime/Show"]
        assert [
            (item.part_type, item.season_number, item.part_number, item.season_label)
            for item in titles
        ] == [
            ("part", 1, 1, "S1"),
            ("part", 1, 2, "S1"),
        ]
        assert {item.catalog_collection_id for item in titles} == {collections[0].id}


def test_plan_updates_title_reassigns_video_and_removes_old_collection():
    engine = _engine()
    with Session(engine) as session:
        old_collection = _collection("Anime/Show/Season 2", "Wrong collection")
        title = _title(
            "Anime/Show/Season 2",
            old_collection,
            "Wrong title",
            part_type="title",
        )
        video = _video(
            "Anime/Show/Season 2/E01.mkv",
            title=title,
            collection=old_collection,
        )
        session.add_all([old_collection, video])
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        collection_actions = _actions(plan.collections)
        title_item = next(item for item in plan.titles if item.title_id == title.id)
        assignment = _assignment(plan, video.relative_path)

        assert collection_actions["Anime/Show"] == ReconciliationAction.CREATE
        assert collection_actions["Anime/Show/Season 2"] == ReconciliationAction.REMOVE
        assert title_item.action == ReconciliationAction.UPDATE
        assert title_item.desired.collection_path == "Anime/Show"
        assert (
            title_item.desired.part_type,
            title_item.desired.season_number,
            title_item.desired.part_number,
        ) == ("season", 2, None)
        assert assignment.changed is True
        assert assignment.target_collection_path == "Anime/Show"
        assert assignment.target_title_path == "Anime/Show/Season 2"

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored_video = session.scalar(select(Video))
        stored_title = session.scalar(select(CatalogTitle))
        assert [item.relative_root_path for item in session.scalars(
            select(CatalogCollection)
        )] == ["Anime/Show"]
        assert stored_title.collection.relative_root_path == "Anime/Show"
        assert stored_video.catalog_title is stored_title
        assert stored_video.catalog_collection is stored_title.collection


def test_cleanup_removes_clean_obsolete_objects_but_preserves_metadata_title():
    engine = _engine()
    with Session(engine) as session:
        clean_collection = _collection("Anime/Clean legacy")
        clean_title = _title("Anime/Clean legacy", clean_collection)
        protected_collection = _collection("Anime/Metadata legacy")
        protected_title = _title("Anime/Metadata legacy", protected_collection)
        protected_title.metadata_record = TitleMetadata(display_title="User metadata")
        session.add_all([clean_collection, clean_title, protected_collection, protected_title])
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        clean_item = next(item for item in plan.titles if item.title_id == clean_title.id)
        protected_item = next(
            item for item in plan.titles if item.title_id == protected_title.id
        )

        assert clean_item.action == ReconciliationAction.REMOVE
        assert clean_item.protected is False
        assert protected_item.action != ReconciliationAction.REMOVE
        assert protected_item.protected is True
        assert "metadata" in protected_item.protection_reasons
        assert any(
            blocker.code == "protected_obsolete_title"
            and blocker.title_path == protected_title.relative_root_path
            and blocker.prevents_apply is False
            for blocker in plan.blockers
        )

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        assert session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Clean legacy"
        )) is None
        protected = session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Metadata legacy"
        ))
        assert protected is not None
        assert protected.metadata_record.display_title == "User metadata"


def test_dry_run_build_and_wrapper_do_not_mutate_database_session_or_timestamps():
    engine = _engine()
    with Session(engine) as session:
        session.add_all([
            _video("Anime/Show/E01.mkv"),
            _video("Anime/Show/E02.mkv", mtime_ns=2),
        ])
        session.commit()
        before = _database_state(session)

        plan = build_hierarchy_rebuild_plan(session)
        dry_result = rebuild_hierarchy(session, apply=False)

        assert plan == dry_result.plan
        assert dry_result.applied is False
        assert plan.has_changes is True
        assert not session.new
        assert not session.dirty
        assert not session.deleted
        assert _database_state(session) == before

    with Session(engine) as session:
        assert _database_state(session) == before


def test_apply_executes_the_exact_dry_run_plan():
    engine = _engine()
    with Session(engine) as session:
        session.add_all([
            _video("Anime/Show/Season 1/E01.mkv"),
            _video("Anime/Show/Season 1/E02.mkv", mtime_ns=2),
        ])
        session.commit()

        dry_result = rebuild_hierarchy(session, apply=False)
        plan = dry_result.plan
        applied = apply_hierarchy_rebuild_plan(session, plan)

        assert applied.applied is True
        assert applied.plan is plan
        assert applied.summary == dry_result.summary
        session.commit()

    with Session(engine) as session:
        assert all(
            video.catalog_title is not None
            and video.catalog_collection is video.catalog_title.collection
            for video in session.scalars(select(Video))
        )


def test_second_rebuild_has_zero_logical_diff_and_does_not_touch_timestamps():
    engine = _engine()
    with Session(engine) as session:
        session.add_all([
            _video("Anime/Show/E01.mkv"),
            _video("Anime/Show/E02.mkv", mtime_ns=2),
        ])
        session.commit()

        first = rebuild_hierarchy(session, apply=True)
        assert first.plan.has_changes is True

        before = _database_state(session)
        second = rebuild_hierarchy(session, apply=False)
        assert second.plan.has_changes is False
        assert second.summary.logical_changes == 0

        applied = apply_hierarchy_rebuild_plan(session, second.plan)
        assert applied.applied is True
        session.commit()
        assert _database_state(session) == before


def test_persistent_explicit_manual_split_unique_moves_video_to_authoritative_target():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection("Anime/Show")
        automatic = _title("Anime/Show", collection, "Old automatic")
        manual = _manual_title(collection, "A", 1)
        video = _video(
            "Anime/Show/E01.mkv",
            title=automatic,
            collection=collection,
        )
        authority = ManualSplitRuleVideo(catalog_title=manual, video=video)
        session.add_all([collection, video, authority])
        session.commit()
        manual_id, video_id = manual.id, video.id

        plan = build_hierarchy_rebuild_plan(session)
        assignment = _assignment(plan, video.relative_path)
        assert assignment.manual_split_kind == "unique"
        assert assignment.target_title_path == manual.relative_root_path
        assert assignment.matched_manual_title_ids == (manual.id,)

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored = session.get(Video, video_id)
        assert stored.catalog_title_id == manual_id
        assert session.get(ManualSplitRuleVideo, (manual_id, video_id)) is not None


def test_explicit_explicit_manual_split_conflict_clears_assignment_and_keeps_authority():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection("Anime/Show")
        first = _manual_title(collection, "A", 1)
        second = _manual_title(collection, "B", 2)
        video = _video("Anime/Show/E01.mkv", title=first, collection=collection)
        session.add_all([
            collection,
            video,
            ManualSplitRuleVideo(catalog_title=first, video=video),
            ManualSplitRuleVideo(catalog_title=second, video=video),
        ])
        session.commit()
        ids = (first.id, second.id, video.id)

        plan = build_hierarchy_rebuild_plan(session)
        assignment = _assignment(plan, video.relative_path)
        assert assignment.manual_split_kind == "conflict"
        assert assignment.target_title_path is None
        assert set(assignment.matched_manual_title_ids) == {first.id, second.id}
        assert any(item.code == "manual_split_conflict" for item in plan.issues)

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        first_id, second_id, video_id = ids
        stored = session.get(Video, video_id)
        assert stored.catalog_title_id is None
        assert stored.catalog_collection_id is not None
        assert session.get(ManualSplitRuleVideo, (first_id, video_id)) is not None
        assert session.get(ManualSplitRuleVideo, (second_id, video_id)) is not None
        assert stored.catalog_collection.hierarchy_status == "conflict"


def test_explicit_range_manual_split_conflict_does_not_degrade_to_unique_range():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection("Anime/Show")
        explicit = _manual_title(collection, "Explicit", 1)
        ranged = _manual_title(
            collection,
            "Range",
            2,
            episode_start=1,
            episode_end=1,
        )
        video = _video("Anime/Show/E01.mkv", title=explicit, collection=collection)
        session.add_all([
            collection,
            video,
            ManualSplitRuleVideo(catalog_title=explicit, video=video),
        ])
        session.commit()
        explicit_id, ranged_id, video_id = explicit.id, ranged.id, video.id

        plan = build_hierarchy_rebuild_plan(session)
        assignment = _assignment(plan, video.relative_path)
        assert assignment.manual_split_kind == "conflict"
        assert set(assignment.matched_manual_title_ids) == {explicit_id, ranged_id}

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored = session.get(Video, video_id)
        assert stored.catalog_title_id is None
        assert session.get(ManualSplitRuleVideo, (explicit_id, video_id)) is not None
        assert session.get(CatalogTitle, ranged_id).episode_start == 1
        assert session.get(CatalogTitle, ranged_id).episode_end == 1


def test_manual_split_unmatched_remains_unassigned_with_structured_issue():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection("Anime/Show")
        target = _manual_title(
            collection,
            "Only E2",
            1,
            episode_start=2,
            episode_end=2,
        )
        video = _video("Anime/Show/E01.mkv")
        session.add_all([collection, target, video])
        session.commit()
        video_id = video.id

        plan = build_hierarchy_rebuild_plan(session)
        assignment = _assignment(plan, video.relative_path)
        assert assignment.manual_split_kind == "unmatched"
        assert assignment.target_collection_path == "Anime/Show"
        assert assignment.target_title_path is None
        assert any(
            item.code == "manual_split_unmatched"
            and item.video_paths == (video.relative_path,)
            for item in plan.issues
        )

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored = session.get(Video, video_id)
        assert stored.catalog_title_id is None
        assert stored.catalog_collection.relative_root_path == "Anime/Show"
        assert stored.catalog_collection.hierarchy_status == "review_required"


@pytest.mark.parametrize(
    ("complete", "expected_status", "expected_issue"),
    [
        (True, "verified", None),
        (False, "review_required", "incomplete_manual_snapshot"),
    ],
    ids=("complete", "historical-incomplete"),
)
def test_manual_hierarchy_snapshot_is_preserved_without_backfill(
    complete,
    expected_status,
    expected_issue,
):
    engine = _engine()
    timestamp = utc_now()
    with Session(engine) as session:
        collection = _collection("Anime/Show")
        title = _title(
            "Anime/Show/.manual-snapshot",
            collection,
            "Manual snapshot",
            part_type="part" if not complete else "season",
            season_number=9,
            part_number=9 if not complete else None,
            season_label="S9",
            hierarchy_manual_override=True,
            part_type_manual="season" if complete else "part",
            season_number_manual=1,
            part_number_manual=None,
            season_label_manual="S1",
            sort_order_manual=1,
            hierarchy_verified_at=timestamp,
        )
        video = _video("Anime/Show/E01.mkv", title=title, collection=collection)
        session.add_all([
            collection,
            video,
            ManualSplitRuleVideo(catalog_title=title, video=video),
        ])
        session.commit()
        title_id = title.id

        plan = build_hierarchy_rebuild_plan(session)
        planned = next(item for item in plan.titles if item.title_id == title_id)
        assert planned.protected is True
        assert "hierarchy_manual_override" in planned.protection_reasons
        issue_codes = {item.code for item in plan.issues}
        if expected_issue is None:
            assert "incomplete_manual_snapshot" not in issue_codes
        else:
            assert expected_issue in issue_codes

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored = session.get(CatalogTitle, title_id)
        assert stored.hierarchy_manual_override is True
        assert stored.season_number_manual == 1
        assert stored.part_number_manual is None
        assert stored.part_type_manual == ("season" if complete else "part")
        assert _without_timezone(stored.hierarchy_verified_at) == _without_timezone(timestamp)
        assert stored.collection.hierarchy_status == expected_status
        if not complete:
            assert stored.part_number == 9


def test_post_4b_grouping_survives_startup_and_rebuild_with_historical_snapshots(
    tmp_path,
):
    """Startup must not reinterpret protected 4B membership as fresh path grouping."""
    engine = create_engine(f"sqlite:///{tmp_path / 'post-4b-grouping.db'}")
    Base.metadata.create_all(engine)
    root_path = "Anime/Example Saga (A01-A04)"
    paths = {
        "season": f"{root_path}/Example Saga (A01)/Example Saga - 01.mkv",
        "related": f"{root_path}/Example Saga Next (A02)/Example Saga Next - 01.mkv",
        "oad": f"{root_path}/OADs/Example Saga - OAD 01.mkv",
        "ova": f"{root_path}/OVA/Example Saga - OVA 01.mkv",
        "special": f"{root_path}/Specials/Example Saga - Special 01.mkv",
    }

    with Session(engine) as session:
        collection = _collection(root_path)
        collection.normalized_local_title = normalize_title(collection.local_title)
        collection.local_period_hint = extract_local_period_hint(collection.local_title)
        titles = {
            "season": _title(
                f"{root_path}/Example Saga (A01)",
                collection,
                part_type="season",
                season_number=1,
                season_label="S1",
            ),
            "related": _title(
                f"{root_path}/.catalog-part-related",
                collection,
                "Example Saga Next",
                part_type="title",
            ),
            "oad": _title(
                f"{root_path}/OADs",
                collection,
                "OADs",
                part_type="ova",
            ),
            "ova": _title(
                f"{root_path}/.catalog-part-ova",
                collection,
                "OVA",
                part_type="ova",
            ),
            "special": _title(
                f"{root_path}/.catalog-part-specials",
                collection,
                "Specials",
                part_type="special",
            ),
        }
        timestamp = utc_now()
        for title in titles.values():
            title.hierarchy_manual_override = True
            title.hierarchy_verified_at = timestamp
        videos = [
            _video(
                path,
                title=titles[kind],
                collection=collection,
                mtime_ns=index,
                file_type=(
                    "ova" if kind in {"oad", "ova"}
                    else "special" if kind == "special"
                    else "episode"
                ),
            )
            for index, (kind, path) in enumerate(paths.items(), 1)
        ]
        session.add_all([collection, *videos])
        session.flush()
        finalize_collection_hierarchy(collection, videos)
        session.commit()
        expected_assignments = {
            video.relative_path: video.catalog_title_id for video in videos
        }

        before_startup = build_hierarchy_rebuild_plan(session)
        assert before_startup.summary.logical_changes == 0
        assert before_startup.summary.video_assignments_changed == 0

    migrate_schema(engine)

    with Session(engine) as session:
        collection_paths = set(session.scalars(
            select(CatalogCollection.relative_root_path)
        ))
        assert collection_paths == {root_path}
        assert {
            video.relative_path: video.catalog_title_id
            for video in session.scalars(select(Video))
        } == expected_assignments
        assert all(
            video.catalog_collection.relative_root_path == root_path
            and video.catalog_title.collection is video.catalog_collection
            for video in session.scalars(select(Video))
        )
        assert all(
            title.hierarchy_manual_override
            and title.hierarchy_verified_at is not None
            and title.part_type_manual is None
            for title in session.scalars(select(CatalogTitle))
        )

        after_startup = build_hierarchy_rebuild_plan(session)
        assert after_startup.summary.logical_changes == 0
        assert after_startup.summary.collections_created == 0
        assert after_startup.summary.titles_created == 0
        assert after_startup.summary.video_assignments_changed == 0
        assert after_startup.summary.numbering_changes == 0


def test_supplementary_classification_is_preserved_and_not_marked_unmatched():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection("Anime/Show")
        target = _manual_title(
            collection,
            "Main",
            1,
            episode_start=1,
            episode_end=1,
        )
        episode = _video("Anime/Show/Show - 01.mkv")
        recap = _video(
            "Anime/Show/Show - Recap 01.mkv",
            mtime_ns=2,
            content_type_manual="recap",
        )
        session.add_all([collection, target, episode, recap])
        session.commit()
        recap_id = recap.id

        plan = build_hierarchy_rebuild_plan(session)
        recap_assignment = _assignment(plan, recap.relative_path)
        assert recap_assignment.manual_split_kind == "not_required"
        assert not any(
            item.code == "manual_split_unmatched"
            and recap.relative_path in item.video_paths
            for item in plan.issues
        )

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored = session.get(Video, recap_id)
        assert stored.content_type_manual == "recap"
        # NOT_REQUIRED is a compatibility boundary: rebuild keeps the current
        # result instead of manufacturing either an automatic or manual match.
        assert stored.catalog_title is None
        assert stored.catalog_collection.relative_root_path == "Anime/Show"


def test_confirmed_secondary_duplicate_is_preserved_and_excluded_from_structure():
    engine = _engine()
    with Session(engine) as session:
        primary = _video("Anime/Show/Show - 01.mkv")
        secondary = _video("Anime/Show/Show 01 copy.mkv", mtime_ns=2)
        second_episode = _video("Anime/Show/Show - 02.mkv", mtime_ns=3)
        secondary.duplicate_of = primary
        session.add_all([primary, secondary, second_episode])
        session.commit()
        primary_id, secondary_id = primary.id, secondary.id

        plan = build_hierarchy_rebuild_plan(session)
        title_item = next(item for item in plan.titles if item.relative_root_path == "Anime/Show")
        assert (title_item.desired.part_type, title_item.desired.season_number) == (
            "season", 1,
        )
        assert "confirmed_duplicate" in {item.code for item in plan.issues}

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored = session.get(Video, secondary_id)
        assert stored.duplicate_of_video_id == primary_id
        assert stored.catalog_title is not None
        assert stored.catalog_collection is stored.catalog_title.collection


def test_missing_duplicate_primary_state_is_preserved_as_structured_problem():
    engine = _engine()
    with Session(engine) as session:
        missing = _video(
            "Anime/Show/Show - 01.mkv",
            duplicate_primary_missing=True,
        )
        session.add_all([missing, _video("Anime/Show/Show - 02.mkv", mtime_ns=2)])
        session.commit()
        missing_id = missing.id

        plan = build_hierarchy_rebuild_plan(session)
        assert "duplicate_primary_missing" in {item.code for item in plan.issues}
        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored = session.get(Video, missing_id)
        assert stored.duplicate_primary_missing is True
        assert stored.duplicate_of_video_id is None


def test_media_part_is_preserved_and_all_redundant_collection_fks_are_consistent():
    engine = _engine()
    with Session(engine) as session:
        first = _video("Anime/Show/E01.mkv", media_part_number=2)
        second = _video("Anime/Show/E02.mkv", mtime_ns=2)
        session.add_all([first, second])
        session.commit()
        first_id = first.id

        result = rebuild_hierarchy(session, apply=True)
        assert result.applied is True

    with Session(engine) as session:
        assert session.get(Video, first_id).media_part_number == 2
        for video in session.scalars(select(Video)):
            assert video.catalog_title is not None
            assert video.catalog_collection is video.catalog_title.collection
            assert video.catalog_collection_id == video.catalog_title.catalog_collection_id


def test_rebuild_plan_contains_final_numbering_gap_issue_and_apply_status():
    engine = _engine()
    with Session(engine) as session:
        session.add_all([
            _video("Anime/Show/E01.mkv"),
            _video("Anime/Show/E03.mkv", mtime_ns=3),
        ])
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        assert "numbering_gap" in {item.code for item in plan.issues}
        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection))
        assert collection.hierarchy_status == "review_required"


def test_direct_root_e1_to_e25_has_long_flat_series_issue():
    engine = _engine()
    with Session(engine) as session:
        session.add_all([
            _video(f"Anime/Show/E{number:02}.mkv", mtime_ns=number)
            for number in range(1, 26)
        ])
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        issue_codes = {item.code for item in plan.issues}
        assert "long_flat_series" in issue_codes
        assert "soft_long_flat_series" not in issue_codes

        title_item = next(item for item in plan.titles if item.relative_root_path == "Anime/Show")
        assert (title_item.desired.part_type, title_item.desired.season_number) == (
            "season", 1,
        )
        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection))
        assert collection.hierarchy_status == "review_required"


def test_explicit_season_two_e1_to_e25_has_no_length_only_issue():
    engine = _engine()
    with Session(engine) as session:
        session.add_all([
            _video(f"Anime/Show/Season 2/E{number:02}.mkv", mtime_ns=number)
            for number in range(1, 26)
        ])
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        issue_codes = {item.code for item in plan.issues}
        assert "long_flat_series" not in issue_codes
        assert "soft_long_flat_series" not in issue_codes
        title_item = next(
            item for item in plan.titles
            if item.relative_root_path == "Anime/Show/Season 2"
        )
        assert (
            title_item.desired.part_type,
            title_item.desired.season_number,
            title_item.desired.part_number,
        ) == ("season", 2, None)

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        collection = session.scalar(select(CatalogCollection))
        assert collection.hierarchy_status == "automatic"


def test_season_one_and_two_reconcile_to_one_collection_and_two_titles():
    engine = _engine()
    paths = [
        f"Anime/Show/Season {season}/E{episode:02}.mkv"
        for season in (1, 2)
        for episode in (1, 2)
    ]
    with Session(engine) as session:
        session.add_all([
            _video(path, mtime_ns=index)
            for index, path in enumerate(paths, 1)
        ])
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        assert plan.summary.collections_created == 1
        assert plan.summary.titles_created == 2
        assert {
            (
                item.desired.collection_path,
                item.desired.season_number,
                item.desired.part_number,
            )
            for item in plan.titles
            if item.desired is not None
        } == {
            ("Anime/Show", 1, None),
            ("Anime/Show", 2, None),
        }

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        collections = list(session.scalars(select(CatalogCollection)))
        titles = list(session.scalars(
            select(CatalogTitle).order_by(CatalogTitle.season_number)
        ))
        assert [item.relative_root_path for item in collections] == ["Anime/Show"]
        assert [
            (item.relative_root_path, item.part_type, item.season_number)
            for item in titles
        ] == [
            ("Anime/Show/Season 1", "season", 1),
            ("Anime/Show/Season 2", "season", 2),
        ]
        for video in session.scalars(select(Video)):
            expected_season = int(PurePosixPath(video.relative_path).parts[2].split()[-1])
            assert video.catalog_title.season_number == expected_season
            assert video.catalog_collection is collections[0]
            assert video.catalog_title.collection is collections[0]


def test_explicit_mn_authority_on_non_manual_title_is_independent_authority():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection("Anime/Show")
        inactive_target = _title("Anime/Show", collection, "Inactive target")
        video = _video("Anime/Show/E01.mkv", title=inactive_target, collection=collection)
        session.add_all([
            collection,
            video,
            ManualSplitRuleVideo(catalog_title=inactive_target, video=video),
        ])
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        assert not any(
            item.code == "inactive_manual_split_authority"
            for item in plan.blockers
        )
        assignment = _assignment(plan, video.relative_path)
        assert assignment.manual_split_kind == "unique"
        assert assignment.target_title_path == inactive_target.relative_root_path


def test_explicit_authority_can_move_video_across_physical_collection_root():
    engine = _engine()
    with Session(engine) as session:
        manual_collection = _collection("Anime/Target Show")
        target = _manual_title(manual_collection, "Selected", 1)
        physical_collection = _collection("Anime/Physical Show")
        physical_title = _title("Anime/Physical Show", physical_collection)
        video = _video(
            "Anime/Physical Show/E01.mkv",
            title=physical_title,
            collection=physical_collection,
        )
        session.add_all([
            manual_collection,
            physical_collection,
            video,
            ManualSplitRuleVideo(catalog_title=target, video=video),
        ])
        session.commit()
        target_id, video_id = target.id, video.id

        plan = build_hierarchy_rebuild_plan(session)
        assignment = _assignment(plan, video.relative_path)
        assert assignment.manual_split_kind == "unique"
        assert assignment.target_collection_path == "Anime/Target Show"
        assert assignment.target_title_path == target.relative_root_path
        assert not any(item.prevents_apply for item in plan.blockers)

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored = session.get(Video, video_id)
        assert stored.catalog_title_id == target_id
        assert stored.catalog_collection.relative_root_path == "Anime/Target Show"
        assert stored.catalog_title.collection is stored.catalog_collection
        assert session.get(ManualSplitRuleVideo, (target_id, video_id)) is not None


def test_range_matching_uses_fresh_filename_numbering_not_stale_derived_values():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection("Anime/Show")
        target = _manual_title(
            collection,
            "Only E1",
            1,
            episode_start=1,
            episode_end=1,
        )
        should_match = _video(
            "Anime/Show/E01.mkv",
            local_episode_number=99,
            season_episode_number=99,
            episode_number_source="stale",
        )
        must_not_match = _video(
            "Anime/Show/E02.mkv",
            mtime_ns=2,
            local_episode_number=1,
            season_episode_number=1,
            episode_number_source="stale",
        )
        session.add_all([collection, target, should_match, must_not_match])
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        matched = _assignment(plan, should_match.relative_path)
        unmatched = _assignment(plan, must_not_match.relative_path)
        assert matched.manual_split_kind == "unique"
        assert matched.target_title_path == target.relative_root_path
        assert unmatched.manual_split_kind == "unmatched"
        assert unmatched.target_title_path is None

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        videos = {
            item.relative_path: item
            for item in session.scalars(select(Video))
        }
        assert videos["Anime/Show/E01.mkv"].catalog_title is not None
        assert videos["Anime/Show/E01.mkv"].local_episode_number == 1
        assert videos["Anime/Show/E02.mkv"].catalog_title is None
        # Unmatched video has no target whose final numbering could own it;
        # importantly, the stale value 1 was not reused for range matching.
        assert videos["Anime/Show/E02.mkv"].local_episode_number is None


def test_invalid_persisted_manual_split_regex_is_structured_hard_blocker():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection("Anime/Show")
        target = _manual_title(
            collection,
            "Invalid pattern",
            1,
            episode_filename_pattern="(",
        )
        video = _video("Anime/Show/E01.mkv")
        session.add_all([collection, target, video])
        session.commit()
        before = _database_state(session)

        plan = build_hierarchy_rebuild_plan(session)
        blocker = next(
            item for item in plan.blockers
            if item.code == "invalid_manual_split_pattern"
        )
        assert blocker.prevents_apply is True
        assert blocker.title_path == target.relative_root_path

        with pytest.raises(HierarchyPlanBlockedError):
            apply_hierarchy_rebuild_plan(session, plan)
        assert _database_state(session) == before


def test_unassigned_root_video_stale_numbering_is_cleared_and_then_idempotent():
    engine = _engine()
    with Session(engine) as session:
        root_video = _video(
            "Loose E07.mkv",
            local_episode_number=99,
            season_episode_number=98,
            absolute_episode_number=97,
            external_episode_number=96,
            episode_number_source="stale",
            episode_number_confidence=0.01,
        )
        session.add(root_video)
        session.commit()
        video_id = root_video.id

        plan = build_hierarchy_rebuild_plan(session)
        assert plan.summary.numbering_changes == 1
        assert plan.summary.video_assignments_changed == 0
        numbering = next(item for item in plan.numbering if item.video_id == video_id)
        assert (
            numbering.desired.local_episode_number,
            numbering.desired.season_episode_number,
            numbering.desired.absolute_episode_number,
            numbering.desired.external_episode_number,
            numbering.desired.episode_number_source,
            numbering.desired.episode_number_confidence,
        ) == (None, None, None, None, "unknown", None)

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

        stored = session.get(Video, video_id)
        assert stored.catalog_collection_id is None
        assert stored.catalog_title_id is None
        assert (
            stored.local_episode_number,
            stored.season_episode_number,
            stored.absolute_episode_number,
            stored.external_episode_number,
            stored.episode_number_source,
            stored.episode_number_confidence,
        ) == (None, None, None, None, "unknown", None)

        second = build_hierarchy_rebuild_plan(session)
        assert second.summary.logical_changes == 0
        assert second.numbering == ()


def test_empty_clean_automatic_obsolete_hierarchy_is_removed_once():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection("Anime/Obsolete")
        title = _title("Anime/Obsolete", collection)
        session.add_all([collection, title])
        session.commit()

        plan = build_hierarchy_rebuild_plan(session)
        collection_item = next(
            item for item in plan.collections
            if item.relative_root_path == collection.relative_root_path
        )
        title_item = next(
            item for item in plan.titles
            if item.relative_root_path == title.relative_root_path
        )
        assert collection_item.action == ReconciliationAction.REMOVE
        assert title_item.action == ReconciliationAction.REMOVE

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

        assert session.scalar(select(CatalogCollection)) is None
        assert session.scalar(select(CatalogTitle)) is None
        second = build_hierarchy_rebuild_plan(session)
        assert second.summary.logical_changes == 0
        assert second.collections == ()
        assert second.titles == ()


@pytest.mark.parametrize(
    "protection_kind",
    ("review-note", "manual-display", "verified"),
)
def test_empty_collection_user_state_and_protected_title_are_exactly_preserved(
    protection_kind,
):
    engine = _engine()
    verified_at = utc_now()
    collection_values = {
        "review-note": {
            "hierarchy_status": "review_required",
            "hierarchy_note": "User-preserved review context",
        },
        "manual-display": {
            "manual_display_title": "Pinned collection name",
        },
        "verified": {
            "hierarchy_status": "verified",
            "hierarchy_verified_at": verified_at,
        },
    }[protection_kind]
    with Session(engine) as session:
        collection = _collection(
            f"Anime/Protected {protection_kind}",
            **collection_values,
        )
        protected_title = _manual_title(collection, "Protected", 1)
        session.add_all([collection, protected_title])
        session.commit()
        collection_id, title_id = collection.id, protected_title.id
        before = _database_state(session)

        plan = build_hierarchy_rebuild_plan(session)
        collection_item = next(
            item for item in plan.collections if item.collection_id == collection_id
        )
        title_item = next(item for item in plan.titles if item.title_id == title_id)
        assert collection_item.current == collection_item.desired
        assert collection_item.action == ReconciliationAction.PRESERVE
        assert title_item.action == ReconciliationAction.PRESERVE
        assert title_item.current == title_item.desired
        assert title_item.protected is True
        assert plan.summary.logical_changes == 0

        result = apply_hierarchy_rebuild_plan(session, plan)
        assert result.applied is True
        session.commit()
        assert _database_state(session) == before

        second = build_hierarchy_rebuild_plan(session)
        assert second.summary.logical_changes == 0
        assert _database_state(session) == before


def test_unrelated_manual_title_without_selector_does_not_freeze_stale_assignment():
    engine = _engine()
    with Session(engine) as session:
        stale_collection = _collection("Anime/Stale")
        stale_title = _title("Anime/Stale", stale_collection, "Stale automatic")
        unrelated_manual = _manual_title(stale_collection, "Unrelated", 9)
        video = _video(
            "Anime/Physical Show/E01.mkv",
            title=stale_title,
            collection=stale_collection,
            local_episode_number=88,
            season_episode_number=88,
            episode_number_source="stale",
        )
        session.add_all([stale_collection, unrelated_manual, video])
        session.commit()
        video_id, manual_id = video.id, unrelated_manual.id

        plan = build_hierarchy_rebuild_plan(session)
        assignment = _assignment(plan, video.relative_path)
        assert assignment.manual_split_kind is None
        assert assignment.target_collection_path == "Anime/Physical Show"
        assert assignment.target_title_path == "Anime/Physical Show"
        assert assignment.changed is True
        stale_item = next(
            item for item in plan.titles
            if item.relative_root_path == stale_title.relative_root_path
        )
        assert stale_item.action == ReconciliationAction.REMOVE

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored = session.get(Video, video_id)
        assert stored.catalog_collection.relative_root_path == "Anime/Physical Show"
        assert stored.catalog_title.relative_root_path == "Anime/Physical Show"
        assert stored.catalog_title.collection is stored.catalog_collection
        manual = session.get(CatalogTitle, manual_id)
        assert manual is not None
        assert manual.hierarchy_manual_override is True
        assert manual.collection.relative_root_path == "Anime/Stale"
        assert build_hierarchy_rebuild_plan(session).summary.logical_changes == 0


def test_pre_4a_conflict_without_explicit_authority_is_hard_blocked():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection(
            "Anime/Show",
            hierarchy_status="conflict",
            hierarchy_note="Historical manual split conflict",
        )
        manual_without_selector = _manual_title(collection, "A", 1)
        ranged = _manual_title(
            collection,
            "B",
            2,
            episode_start=1,
            episode_end=12,
        )
        video = _video("Anime/Show/E01.mkv", collection=collection)
        session.add_all([collection, manual_without_selector, ranged, video])
        session.commit()
        before = _database_state(session)

        plan = build_hierarchy_rebuild_plan(session)
        blocker = next(
            item for item in plan.blockers
            if item.code == "historical_pre4a_manual_split_conflict"
        )
        assert blocker.prevents_apply is True
        assert blocker.collection_path == "Anime/Show"
        assert blocker.video_path == video.relative_path
        assignment = _assignment(plan, video.relative_path)
        assert assignment.manual_split_kind is None
        assert assignment.target_title_path is None
        assert session.scalar(select(ManualSplitRuleVideo)) is None

        with pytest.raises(HierarchyPlanBlockedError):
            apply_hierarchy_rebuild_plan(session, plan)
        assert _database_state(session) == before


def test_unrelated_manual_target_in_same_collection_does_not_freeze_stale_title():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection("Anime/Show")
        stale_title = _title(
            "Anime/Show/.stale-automatic",
            collection,
            "Stale automatic",
            part_type="bonus",
        )
        unrelated_manual = _manual_title(collection, "Unrelated", 9)
        video = _video(
            "Anime/Show/Season 2/E01.mkv",
            title=stale_title,
            collection=collection,
            local_episode_number=88,
            season_episode_number=88,
            episode_number_source="stale",
        )
        session.add_all([collection, unrelated_manual, video])
        session.commit()
        video_id = video.id

        plan = build_hierarchy_rebuild_plan(session)
        assignment = _assignment(plan, video.relative_path)
        assert assignment.manual_split_kind is None
        assert assignment.reason == ReconciliationReason.AUTOMATIC_PATH
        assert assignment.target_collection_path == "Anime/Show"
        assert assignment.target_title_path == "Anime/Show/Season 2"
        assert assignment.changed is True
        stale_item = next(
            item for item in plan.titles
            if item.relative_root_path == stale_title.relative_root_path
        )
        rebuilt_item = next(
            item for item in plan.titles
            if item.relative_root_path == "Anime/Show/Season 2"
        )
        assert stale_item.action == ReconciliationAction.REMOVE
        assert rebuilt_item.action == ReconciliationAction.CREATE
        assert (
            rebuilt_item.desired.part_type,
            rebuilt_item.desired.season_number,
            rebuilt_item.desired.part_number,
        ) == ("season", 2, None)

        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()

    with Session(engine) as session:
        stored = session.get(Video, video_id)
        assert stored.catalog_collection.relative_root_path == "Anime/Show"
        assert stored.catalog_title.relative_root_path == "Anime/Show/Season 2"
        assert (
            stored.catalog_title.part_type,
            stored.catalog_title.season_number,
            stored.catalog_title.part_number,
        ) == ("season", 2, None)
        assert stored.catalog_title.collection is stored.catalog_collection
        assert session.scalar(select(CatalogTitle).where(
            CatalogTitle.relative_root_path == "Anime/Show/.stale-automatic"
        )) is None


def test_pre_4a_guard_is_per_video_beside_reproducible_conflict():
    engine = _engine()
    with Session(engine) as session:
        collection = _collection(
            "Anime/Show",
            hierarchy_status="conflict",
            hierarchy_note="Historical manual split conflict",
        )
        explicit = _manual_title(collection, "A", 1)
        ranged = _manual_title(
            collection,
            "B",
            2,
            episode_start=1,
            episode_end=2,
        )
        reproducible_conflict = _video("Anime/Show/E01.mkv", collection=collection)
        historical_unknown = _video(
            "Anime/Show/E02.mkv",
            collection=collection,
            mtime_ns=2,
        )
        session.add_all([
            collection,
            reproducible_conflict,
            historical_unknown,
            ManualSplitRuleVideo(
                catalog_title=explicit,
                video=reproducible_conflict,
            ),
        ])
        session.commit()
        before = _database_state(session)

        plan = build_hierarchy_rebuild_plan(session)
        first = _assignment(plan, reproducible_conflict.relative_path)
        second = _assignment(plan, historical_unknown.relative_path)
        assert first.manual_split_kind == "conflict"
        assert first.target_title_path is None
        assert set(first.matched_manual_title_ids) == {explicit.id, ranged.id}
        assert second.manual_split_kind is None
        assert second.target_title_path is None

        historical_blockers = [
            item for item in plan.blockers
            if item.code == "historical_pre4a_manual_split_conflict"
        ]
        assert [item.video_path for item in historical_blockers] == [
            historical_unknown.relative_path
        ]
        assert all(item.prevents_apply for item in historical_blockers)

        with pytest.raises(HierarchyPlanBlockedError):
            apply_hierarchy_rebuild_plan(session, plan)
        assert _database_state(session) == before


@pytest.mark.parametrize("lifecycle", ("scan", "startup"))
@pytest.mark.parametrize(
    ("target_count", "expected_kind", "expected_status"),
    ((1, "unique", "verified"), (2, "conflict", "conflict")),
    ids=("unique", "explicit-explicit-conflict"),
)
def test_cross_physical_explicit_authority_survives_scan_and_startup(
    tmp_path,
    monkeypatch,
    lifecycle,
    target_count,
    expected_kind,
    expected_status,
):
    library = tmp_path / "library"
    physical_file = library / "Anime/Physical Show/E01.mkv"
    physical_file.parent.mkdir(parents=True)
    physical_file.write_bytes(b"video")
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'cross-physical.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        manual_collection = _collection("Anime/Manual Target")
        targets = [
            _manual_title(manual_collection, chr(ord("A") + index), index + 1)
            for index in range(target_count)
        ]
        physical_collection = _collection("Anime/Physical Show")
        physical_title = _title("Anime/Physical Show", physical_collection)
        video = _video(
            "Anime/Physical Show/E01.mkv",
            title=physical_title,
            collection=physical_collection,
        )
        session.add_all([
            manual_collection,
            physical_collection,
            video,
            *[
                ManualSplitRuleVideo(catalog_title=target, video=video)
                for target in targets
            ],
        ])
        session.commit()
        video_id = video.id
        target_ids = tuple(target.id for target in targets)

        initial_plan = build_hierarchy_rebuild_plan(session)
        initial_assignment = _assignment(initial_plan, video.relative_path)
        assert initial_assignment.manual_split_kind == expected_kind
        assert initial_assignment.target_collection_path == "Anime/Manual Target"
        assert initial_assignment.target_title_path == (
            targets[0].relative_root_path if target_count == 1 else None
        )
        apply_hierarchy_rebuild_plan(session, initial_plan)
        session.commit()

    def assert_authoritative_state() -> None:
        with Session(engine) as session:
            video = session.get(Video, video_id)
            assert video.catalog_collection.relative_root_path == "Anime/Manual Target"
            assert video.catalog_title_id == (target_ids[0] if target_count == 1 else None)
            assert video.catalog_collection.hierarchy_status == expected_status
            assert tuple(session.scalars(
                select(ManualSplitRuleVideo.catalog_title_id).where(
                    ManualSplitRuleVideo.video_id == video_id
                ).order_by(ManualSplitRuleVideo.catalog_title_id)
            )) == target_ids

    assert_authoritative_state()

    if lifecycle == "scan":
        with Session(engine) as session:
            scan_library(session, library)
    else:
        migrate_schema(engine)

    assert_authoritative_state()
    with Session(engine) as session:
        final_plan = build_hierarchy_rebuild_plan(session)
        assert final_plan.summary.logical_changes == 0
        final_assignment = _assignment(final_plan, "Anime/Physical Show/E01.mkv")
        assert final_assignment.manual_split_kind == expected_kind
        assert final_assignment.target_collection_path == "Anime/Manual Target"
        assert final_assignment.target_title_path == (
            "Anime/Manual Target/.manual-a" if target_count == 1 else None
        )
