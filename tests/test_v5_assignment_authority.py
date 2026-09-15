"""Regression coverage for V5 R1/R2 assignment authority simplification.

R1: a title's manual hierarchy snapshot is structural authority over that title
and never membership authority over a video found under its path.

R2: releasing explicit selectors must finalize every released video exactly
once, from an immutable working set, and must never create a collection or
title in order to have somewhere to put a video.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.hierarchy_review as hierarchy_review
from app.database import Base, make_engine, make_session_factory
from app.hierarchy_authority import activate_manual_hierarchy_snapshot
from app.hierarchy_rebuild import build_hierarchy_rebuild_plan
from app.hierarchy_review import (
    ManualTitleDefinition,
    apply_manual_split,
    record_manual_collection_merge,
)
from app.models import CatalogCollection, CatalogTitle, Video, utc_now
from app.scanner import scan_library
from app.unassigned_videos import insufficient_video_assignment


PROBE_RESULT = {
    "duration": 60.0,
    "video_codec": "h264",
    "width": 1920,
    "height": 1080,
    "audio": [],
    "subtitles": [],
}


def _selector(
    title: CatalogTitle,
    video_ids: tuple[int, ...] = (),
    *,
    episode_start: int | None = None,
    episode_end: int | None = None,
) -> ManualTitleDefinition:
    return ManualTitleDefinition(
        title_id=title.id,
        local_title=title.local_title,
        manual_display_title=title.manual_display_title,
        season_number_manual=None,
        season_label_manual=None,
        part_number_manual=None,
        part_type_manual=None,
        episode_start=episode_start,
        episode_end=episode_end,
        episode_start_offset=title.episode_start_offset,
        numbering_mode=title.numbering_mode,
        sort_order=None,
        filename_pattern=None,
        video_ids=video_ids,
    )


def _collection(path: str, name: str) -> CatalogCollection:
    return CatalogCollection(
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=path,
    )


def _title(
    collection: CatalogCollection,
    path: str,
    name: str,
    season_number: int,
) -> CatalogTitle:
    return CatalogTitle(
        collection=collection,
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=path,
        part_type="season",
        season_number=season_number,
        season_label=f"S{season_number}",
        sort_order=season_number,
    )


def _video(
    session: Session,
    path: str,
    collection: CatalogCollection,
    title: CatalogTitle,
    number: int,
) -> Video:
    video = Video(
        relative_path=path,
        root_folder="Anime",
        filename=path.rsplit("/", 1)[-1],
        size=number,
        mtime_ns=number,
        file_type="episode",
        catalog_collection=collection,
        catalog_title=title,
    )
    session.add(video)
    return video


def _merged_snapshot_library(
    session: Session,
    *,
    count: int = 3,
    snapshot: bool = True,
    merged: bool = True,
) -> tuple[CatalogCollection, CatalogCollection, CatalogTitle, CatalogTitle, list[Video]]:
    """Physical Season 1 title verified by a human and moved by a confirmed merge.

    The videos physically belong under that title but are explicitly pinned into
    an unrelated automatic Season 2 that lives in the ordinary path collection.
    """
    source = _collection("Anime/Show", "Show")
    merge_target = _collection("@manual/show", "Show manual")
    physical = _title(
        merge_target if merged else source,
        "Anime/Show/Season 1",
        "Season 1",
        1,
    )
    pinned = _title(source, "Anime/Show/Season 2", "Season 2", 2)
    videos = [
        _video(session, f"Anime/Show/Season 1/E{n:02}.mkv", source, pinned, n)
        for n in range(1, count + 1)
    ]
    session.add_all([source, merge_target])
    session.flush()
    if snapshot:
        activate_manual_hierarchy_snapshot(
            physical,
            part_type="season",
            season_number=1,
            part_number=None,
            season_label="S1",
            sort_order=1,
            verified_at=utc_now(),
        )
    if merged:
        record_manual_collection_merge(session, merge_target, [physical.id])
    session.flush()
    return source, merge_target, physical, pinned, videos


# --------------------------------------------------------------------------
# R1 - structural snapshot is not membership authority
# --------------------------------------------------------------------------


def test_r1_released_video_follows_its_own_path_not_a_foreign_manual_snapshot(tmp_path):
    """The existing path target decides, and the result stays internally consistent."""
    engine = make_engine(f"sqlite:///{tmp_path / 'r1-path.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source, merge_target, physical, pinned, videos = _merged_snapshot_library(session)
        apply_manual_split(
            session, source.id, [_selector(pinned, tuple(v.id for v in videos))],
        )
        session.flush()

        apply_manual_split(session, source.id, [_selector(pinned)])
        session.flush()

        for video in videos:
            assert video.catalog_title is physical
            # The merged collection follows the title, so the video never ends
            # up in the contradictory "collection of one title, membership of
            # another" state the old helper produced.
            assert video.catalog_collection is merge_target
            assert insufficient_video_assignment(video) is None
    engine.dispose()


def test_r1_same_decision_without_any_manual_snapshot(tmp_path):
    """Identical outcome proves the decision came from the path, not from manual-ness."""
    engine = make_engine(f"sqlite:///{tmp_path / 'r1-plain.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source, _merge_target, physical, pinned, videos = _merged_snapshot_library(
            session, count=1, snapshot=False, merged=False,
        )
        apply_manual_split(
            session, source.id, [_selector(pinned, (videos[0].id,))],
        )
        session.flush()
        apply_manual_split(session, source.id, [_selector(pinned)])
        session.flush()

        assert videos[0].catalog_title is physical
        assert videos[0].catalog_collection is source
    engine.dispose()


def test_r1_manual_snapshot_alone_never_pulls_in_a_video_from_another_path(tmp_path):
    """A verified title must not absorb a released video that is not under its path."""
    engine = make_engine(f"sqlite:///{tmp_path / 'r1-absorb.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = _collection("Anime/Show", "Show")
        verified = _title(collection, "Anime/Show/Season 1", "Season 1", 1)
        pinned = _title(collection, "Anime/Show/Season 2", "Season 2", 2)
        # The released video lives under a third, unmaterialized physical path.
        video = _video(
            session, "Anime/Show/Season 3/E01.mkv", collection, pinned, 1,
        )
        session.add(collection)
        session.flush()
        activate_manual_hierarchy_snapshot(
            verified,
            part_type="season",
            season_number=1,
            part_number=None,
            season_label="S1",
            sort_order=1,
            verified_at=utc_now(),
        )
        session.flush()
        apply_manual_split(session, collection.id, [_selector(pinned, (video.id,))])
        session.flush()
        titles_before = set(session.scalars(select(CatalogTitle.relative_root_path)))

        apply_manual_split(session, collection.id, [_selector(pinned)])
        session.flush()

        assert video.catalog_title is None
        assert video.catalog_collection is collection
        assert insufficient_video_assignment(video).code == "missing_catalog_title"
        assert set(session.scalars(select(CatalogTitle.relative_root_path))) == titles_before
    engine.dispose()


def test_r1_preserved_own_manual_placement_is_still_respected(tmp_path):
    """Protected membership the video already has is not re-derived from the path."""
    engine = make_engine(f"sqlite:///{tmp_path / 'r1-preserve.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = _collection("Anime/Show", "Show")
        physical = _title(collection, "Anime/Show/Season 1", "Season 1", 1)
        verified = _title(collection, "Anime/Show/.catalog-part-1", "Extras", 2)
        video = _video(
            session, "Anime/Show/Season 1/E01.mkv", collection, verified, 1,
        )
        session.add(collection)
        session.flush()
        activate_manual_hierarchy_snapshot(
            verified,
            part_type="special",
            season_number=None,
            part_number=None,
            season_label=None,
            sort_order=2,
            verified_at=utc_now(),
        )
        session.flush()
        apply_manual_split(session, collection.id, [_selector(verified, (video.id,))])
        session.flush()

        apply_manual_split(session, collection.id, [_selector(verified)])
        session.flush()

        assert video.catalog_title is verified
        assert physical.videos == []
    engine.dispose()


# --------------------------------------------------------------------------
# R2 - bulk release works on an immutable snapshot and creates nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("count", [3, 6])
def test_r2_every_released_video_is_reconciled_exactly_once(tmp_path, monkeypatch, count):
    engine = make_engine(f"sqlite:///{tmp_path / f'r2-bulk-{count}.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source, merge_target, physical, pinned, videos = _merged_snapshot_library(
            session, count=count,
        )
        apply_manual_split(
            session, source.id, [_selector(pinned, tuple(v.id for v in videos))],
        )
        session.flush()
        video_ids = [video.id for video in videos]

        seen: list[int] = []
        original = hierarchy_review.automatic_assignment_title

        def spy(identity, titles_by_path):
            seen.append(identity.title.relative_root_path)
            return original(identity, titles_by_path)

        monkeypatch.setattr(hierarchy_review, "automatic_assignment_title", spy)
        apply_manual_split(session, source.id, [_selector(pinned)])
        session.flush()

        assert len(seen) == count
        assert {
            video.catalog_title_id for video in videos
        } == {physical.id}
        assert {
            video.catalog_collection_id for video in videos
        } == {merge_target.id}
        assert sorted(video_ids) == sorted(video.id for video in videos)
    engine.dispose()


def test_r2_release_never_creates_a_collection_or_title(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'r2-nocreate.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        manual = _collection("@manual/show", "Show")
        pinned = CatalogTitle(
            collection=manual,
            local_title="Part A",
            normalized_local_title="part a",
            relative_root_path="@manual/show/.catalog-part-1",
            part_type="season",
            season_number=1,
            sort_order=1,
        )
        videos = [
            _video(session, f"Anime/Show/Season 1/E{n:02}.mkv", manual, pinned, n)
            for n in (1, 2, 3)
        ]
        session.add(manual)
        session.flush()
        apply_manual_split(
            session, manual.id, [_selector(pinned, tuple(v.id for v in videos))],
        )
        session.flush()
        collections_before = set(
            session.scalars(select(CatalogCollection.relative_root_path))
        )
        titles_before = set(session.scalars(select(CatalogTitle.relative_root_path)))

        apply_manual_split(session, manual.id, [_selector(pinned)])
        session.flush()

        assert set(
            session.scalars(select(CatalogCollection.relative_root_path))
        ) == collections_before
        assert set(
            session.scalars(select(CatalogTitle.relative_root_path))
        ) == titles_before
        for video in videos:
            assert video.catalog_title is None
            assert video.catalog_collection is manual
            assert insufficient_video_assignment(video).code == "missing_catalog_title"
    engine.dispose()


@pytest.mark.parametrize("reversed_order", [False, True])
def test_r2_mixed_release_outcomes_are_independent_of_processing_order(
    tmp_path, reversed_order,
):
    engine = make_engine(f"sqlite:///{tmp_path / f'r2-mixed-{reversed_order}.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = _collection("Anime/Show", "Show")
        physical = _title(collection, "Anime/Show/Season 1", "Season 1", 1)
        released = _title(collection, "Anime/Show/Season 2", "Season 2", 2)
        other = _title(collection, "Anime/Show/.catalog-part-other", "Other", 3)
        # a: existing unique automatic target, b: no existing target at all,
        # c: another explicit selector that still covers the video.
        order = (3, 2, 1) if reversed_order else (1, 2, 3)
        video_a = _video(
            session, "Anime/Show/Season 1/E01.mkv", collection, released, order[0],
        )
        video_b = _video(
            session, "Anime/Show/Season 9/E02.mkv", collection, released, order[1],
        )
        video_c = _video(
            session, "Anime/Show/Season 1/E03.mkv", collection, released, order[2],
        )
        session.add(collection)
        session.flush()
        apply_manual_split(
            session,
            collection.id,
            [_selector(released, (video_a.id, video_b.id, video_c.id))],
        )
        session.flush()
        titles_before = set(session.scalars(select(CatalogTitle.relative_root_path)))

        apply_manual_split(
            session,
            collection.id,
            [_selector(released), _selector(other, (video_c.id,))],
        )
        session.flush()

        assert video_a.catalog_title is physical
        assert video_b.catalog_title is None
        assert insufficient_video_assignment(video_b).code == "missing_catalog_title"
        assert video_c.catalog_title is other
        assert set(
            session.scalars(select(CatalogTitle.relative_root_path))
        ) == titles_before
    engine.dispose()


def test_r2_collection_scope_authority_keeps_its_semantics_after_a_release(tmp_path):
    """A surviving range rule stays authoritative for the whole collection."""
    engine = make_engine(f"sqlite:///{tmp_path / 'r2-range.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = _collection("Anime/Show", "Show")
        _title(collection, "Anime/Show/Season 1", "Season 1", 1)
        released = _title(collection, "Anime/Show/Season 2", "Season 2", 2)
        ranged = _title(collection, "Anime/Show/.catalog-part-range", "Ranged", 3)
        matched = _video(session, "Anime/Show/Season 1/E04.mkv", collection, released, 4)
        unmatched = _video(session, "Anime/Show/Season 1/E05.mkv", collection, released, 5)
        session.add(collection)
        session.flush()
        apply_manual_split(
            session,
            collection.id,
            [_selector(released, (matched.id, unmatched.id))],
        )
        session.flush()
        titles_before = set(session.scalars(select(CatalogTitle.relative_root_path)))

        apply_manual_split(
            session,
            collection.id,
            [_selector(released), _selector(ranged, episode_start=4, episode_end=4)],
        )
        session.flush()

        assert matched.catalog_title is ranged
        # Collection-scope authority owns the collection: the video it does not
        # cover goes to review instead of being guessed onto a path title.
        assert unmatched.catalog_title is None
        assert set(
            session.scalars(select(CatalogTitle.relative_root_path))
        ) == titles_before
    engine.dispose()


def test_r2_failed_reconciliation_restores_selectors_assignments_and_structures(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'r2-rollback.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions() as session:
        source, _merge, _physical, pinned, videos = _merged_snapshot_library(
            session, count=4,
        )
        apply_manual_split(
            session, source.id, [_selector(pinned, tuple(v.id for v in videos))],
        )
        session.commit()
        source_id = source.id
        pinned_id = pinned.id
        video_ids = [video.id for video in videos]
        collections_before = set(
            session.scalars(select(CatalogCollection.relative_root_path))
        )
        titles_before = set(session.scalars(select(CatalogTitle.relative_root_path)))

    calls = {"n": 0}
    original = hierarchy_review.automatic_assignment_title

    def fail_after_two(identity, titles_by_path):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("simulated released-assignment failure")
        return original(identity, titles_by_path)

    with sessions() as session:
        pinned = session.get(CatalogTitle, pinned_id)
        hierarchy_review.automatic_assignment_title = fail_after_two
        try:
            with pytest.raises(RuntimeError, match="simulated"):
                apply_manual_split(session, source_id, [_selector(pinned)])
        finally:
            hierarchy_review.automatic_assignment_title = original
        session.rollback()

    with sessions() as session:
        for video_id in video_ids:
            video = session.get(Video, video_id)
            assert video.catalog_title_id == pinned_id
            assert [
                link.catalog_title_id for link in video.manual_split_rule_videos
            ] == [pinned_id]
        assert set(
            session.scalars(select(CatalogCollection.relative_root_path))
        ) == collections_before
        assert set(
            session.scalars(select(CatalogTitle.relative_root_path))
        ) == titles_before
    engine.dispose()


# --------------------------------------------------------------------------
# Lifecycle parity and review persistence
# --------------------------------------------------------------------------


def _write_library(root: Path, seasons: dict[str, int]) -> None:
    for season, episodes in seasons.items():
        for number in range(1, episodes + 1):
            path = root / "Anime" / "Show" / season / f"E{number:02}.mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")


def test_release_parity_across_reload_rebuild_and_scanner(tmp_path, monkeypatch):
    library = tmp_path / "library"
    _write_library(library, {"Season 1": 2, "Season 2": 1})
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_a, **_k: PROBE_RESULT,
    )
    engine = make_engine(f"sqlite:///{tmp_path / 'parity.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)

    with sessions() as session:
        scan_library(session, library)
        titles = {
            title.local_title: title
            for title in session.scalars(select(CatalogTitle))
        }
        season_1, season_2 = titles["Season 1"], titles["Season 2"]
        released = sorted(season_1.videos, key=lambda video: video.filename)
        apply_manual_split(
            session,
            season_2.catalog_collection_id,
            [_selector(season_2, tuple(video.id for video in released))],
        )
        session.commit()
        collection_id = season_2.catalog_collection_id
        season_1_id = season_1.id
        released_ids = [video.id for video in released]

    with sessions() as session:
        season_2 = next(
            title for title in session.get(CatalogCollection, collection_id).titles
            if title.local_title == "Season 2"
        )
        apply_manual_split(session, collection_id, [_selector(season_2)])
        for video_id in released_ids:
            assert session.get(Video, video_id).catalog_title_id == season_1_id
        session.commit()

    with sessions() as session:
        plan = build_hierarchy_rebuild_plan(session)
        for video_id in released_ids:
            item = next(
                entry for entry in plan.video_assignments if entry.video_id == video_id
            )
            assert item.changed is False
            assert item.target_title_path == "Anime/Show/Season 1"
        scan_library(session, library)

    with sessions() as session:
        for video_id in released_ids:
            assert session.get(Video, video_id).catalog_title_id == season_1_id
    engine.dispose()


def test_unassigned_release_stays_in_review_across_reload_and_rebuild(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'review-persist.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions() as session:
        manual = _collection("@manual/show", "Show")
        pinned = CatalogTitle(
            collection=manual,
            local_title="Part A",
            normalized_local_title="part a",
            relative_root_path="@manual/show/.catalog-part-1",
            part_type="season",
            season_number=1,
            sort_order=1,
        )
        video = _video(session, "Anime/Show/Season 1/E01.mkv", manual, pinned, 1)
        session.add(manual)
        session.flush()
        apply_manual_split(session, manual.id, [_selector(pinned, (video.id,))])
        apply_manual_split(session, manual.id, [_selector(pinned)])
        session.commit()
        video_id = video.id

    with sessions() as session:
        stored = session.get(Video, video_id)
        assert stored.catalog_title_id is None
        assert insufficient_video_assignment(stored).code == "missing_catalog_title"
        plan = build_hierarchy_rebuild_plan(session)
        item = next(
            entry for entry in plan.video_assignments if entry.video_id == video_id
        )
        # A rebuild may propose the physical structure, but it never silently
        # commits it: the video stays unassigned until a human or a real scan
        # supplies the missing evidence.
        assert session.get(Video, video_id).catalog_title_id is None
        assert item.target_title_path in (None, "Anime/Show/Season 1")
    engine.dispose()


def test_review_is_resolved_only_by_new_physical_evidence_not_by_re_inference(
    tmp_path, monkeypatch,
):
    """A scan may materialize real structure; it never re-decides an ambiguity.

    The release refused to place these videos because the target did not exist,
    not because two readings competed.  A real scan of the library is new
    evidence: the folders are actually there, so the scanner creates the
    structure it found and the review item is answered by facts.  The ambiguous
    case below has no such answer and stays in review.
    """
    library = tmp_path / "library"
    _write_library(library, {"Season 1": 1})
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_a, **_k: PROBE_RESULT,
    )
    engine = make_engine(f"sqlite:///{tmp_path / 'review-scan.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions() as session:
        manual = _collection("@manual/show", "Show")
        pinned = CatalogTitle(
            collection=manual,
            local_title="Part A",
            normalized_local_title="part a",
            relative_root_path="@manual/show/.catalog-part-1",
            part_type="season",
            season_number=1,
            sort_order=1,
        )
        video = _video(session, "Anime/Show/Season 1/E01.mkv", manual, pinned, 1)
        session.add(manual)
        session.flush()
        apply_manual_split(session, manual.id, [_selector(pinned, (video.id,))])
        apply_manual_split(session, manual.id, [_selector(pinned)])
        session.commit()
        video_id = video.id
        assert session.get(Video, video_id).catalog_title_id is None

    with sessions() as session:
        scan_library(session, library)
        session.commit()

    with sessions() as session:
        stored = session.get(Video, video_id)
        assert stored.catalog_title.relative_root_path == "Anime/Show/Season 1"
        assert insufficient_video_assignment(stored) is None


def test_ambiguous_release_outcome_is_not_resolved_by_a_later_scan(tmp_path, monkeypatch):
    """Collection-scope authority that covers nothing keeps the video in review."""
    library = tmp_path / "library"
    _write_library(library, {"Season 1": 2})
    monkeypatch.setattr(
        "app.scanner.service.probe_video", lambda *_a, **_k: PROBE_RESULT,
    )
    engine = make_engine(f"sqlite:///{tmp_path / 'review-ambiguous.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions() as session:
        scan_library(session, library)
        collection = session.scalar(select(CatalogCollection))
        season_1 = session.scalar(
            select(CatalogTitle).where(
                CatalogTitle.relative_root_path == "Anime/Show/Season 1"
            )
        )
        videos = sorted(season_1.videos, key=lambda item: item.filename)
        ranged = _title(collection, "Anime/Show/.catalog-part-range", "Ranged", 9)
        session.add(ranged)
        session.flush()
        apply_manual_split(
            session,
            collection.id,
            [_selector(season_1, tuple(video.id for video in videos))],
        )
        apply_manual_split(
            session,
            collection.id,
            [_selector(season_1), _selector(ranged, episode_start=1, episode_end=1)],
        )
        session.commit()
        unmatched_id = videos[1].id
        assert session.get(Video, unmatched_id).catalog_title_id is None

    with sessions() as session:
        scan_library(session, library)
        session.commit()

    with sessions() as session:
        stored = session.get(Video, unmatched_id)
        assert stored.catalog_title_id is None
        assert insufficient_video_assignment(stored).code == "missing_catalog_title"
