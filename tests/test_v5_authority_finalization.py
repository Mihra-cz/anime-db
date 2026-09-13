"""Regression coverage for V5 authority-write projection finalization."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.hierarchy_review as hierarchy_review
import app.metadata.service as metadata_service
from app.config import Settings
from app.database import Base, make_engine, make_session_factory
from app.hierarchy_authority import activate_manual_hierarchy_snapshot
from app.hierarchy_rebuild import build_hierarchy_rebuild_plan
from app.hierarchy_review import (
    ManualTitleDefinition,
    apply_manual_split,
    record_manual_collection_merge,
    refresh_collection_state,
)
from app.manual_split import ManualSplitDecisionKind, evaluate_persisted_manual_split
from app.metadata.providers.base import ProviderTitleMetadata
from app.metadata.service import (
    confirm_anilist_candidate,
    refresh_title_metadata,
    unlink_title_metadata,
)
from app.main import create_app
from app.models import (
    CatalogCollection,
    CatalogTitle,
    ExternalTitleLink,
    TitleMetadata,
    Video,
    utc_now,
)
from app.numbering import set_video_episode_override
from app.scanner import scan_library


PROBE_RESULT = {
    "duration": 60.0,
    "video_codec": "h264",
    "width": 1920,
    "height": 1080,
    "audio": [],
    "subtitles": [],
}


class Provider:
    def __init__(self, records: dict[str, ProviderTitleMetadata]):
        self.records = records

    def fetch_title(self, external_id: str) -> ProviderTitleMetadata:
        return self.records[str(external_id)]


def _metadata(external_id: str, *, episodes: int | None) -> ProviderTitleMetadata:
    return ProviderTitleMetadata(
        provider="anilist",
        external_id=external_id,
        title_romaji=f"Show {external_id}",
        episode_count=episodes,
        site_url=f"https://anilist.co/anime/{external_id}",
    )


def _selector_definition(
    title: CatalogTitle,
    video_ids: tuple[int, ...] = (),
    *,
    episode_start: int | None = None,
    episode_end: int | None = None,
    filename_pattern: str | None = None,
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
        filename_pattern=filename_pattern,
        video_ids=video_ids,
    )


def _write_two_season_library(root: Path) -> None:
    for season in (1, 2):
        path = root / "Anime" / "Show" / f"Season {season}" / "E01.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")


def _numbering_collection(
    session: Session,
    *,
    downstream_number: int = 3,
) -> tuple[CatalogCollection, CatalogTitle, CatalogTitle]:
    collection = CatalogCollection(
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    season_1 = CatalogTitle(
        collection=collection,
        local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1",
        part_type="season",
        season_number=1,
        season_label="S1",
        sort_order=1,
    )
    season_2 = CatalogTitle(
        collection=collection,
        local_title="Season 2",
        normalized_local_title="season 2",
        relative_root_path="Anime/Show/Season 2",
        part_type="season",
        season_number=2,
        season_label="S2",
        sort_order=2,
    )
    for number in (1, 2):
        session.add(Video(
            relative_path=f"{season_1.relative_root_path}/E{number:02}.mkv",
            root_folder="Anime",
            filename=f"E{number:02}.mkv",
            size=number,
            mtime_ns=number,
            file_type="episode",
            catalog_collection=collection,
            catalog_title=season_1,
        ))
    session.add(Video(
        relative_path=f"{season_2.relative_root_path}/E{downstream_number:02}.mkv",
        root_folder="Anime",
        filename=f"E{downstream_number:02}.mkv",
        size=downstream_number,
        mtime_ns=downstream_number,
        file_type="episode",
        catalog_collection=collection,
        catalog_title=season_2,
    ))
    session.add(collection)
    session.flush()
    return collection, season_1, season_2


def _manual_split_collection(
    session: Session,
) -> tuple[CatalogCollection, CatalogTitle, CatalogTitle, CatalogTitle, Video]:
    collection = CatalogCollection(
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    titles = [
        CatalogTitle(
            collection=collection,
            local_title=f"Season {number}",
            normalized_local_title=f"season {number}",
            relative_root_path=f"Anime/Show/Season {number}",
            part_type="season",
            season_number=number,
            season_label=f"S{number}",
            sort_order=number,
        )
        for number in (1, 2, 3)
    ]
    video = Video(
        relative_path="Anime/Show/Season 1/E01.mkv",
        root_folder="Anime",
        filename="E01.mkv",
        size=1,
        mtime_ns=1,
        file_type="episode",
        catalog_collection=collection,
        catalog_title=titles[0],
    )
    session.add(collection)
    session.flush()
    return collection, titles[0], titles[1], titles[2], video


def _pin_video(
    session: Session,
    collection: CatalogCollection,
    target: CatalogTitle,
    video: Video,
) -> None:
    apply_manual_split(
        session,
        collection.id,
        [_selector_definition(target, (video.id,))],
    )
    session.flush()
    assert video.catalog_title_id == target.id


def test_b2_removed_explicit_pin_is_finalized_before_return_and_stays_stable(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "library"
    _write_two_season_library(library)
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = make_engine(f"sqlite:///{tmp_path / 'b2-parity.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)

    with sessions() as session:
        scan_library(session, library)
        titles = {
            title.local_title: title for title in session.scalars(select(CatalogTitle))
        }
        video = next(
            item for item in titles["Season 1"].videos if item.filename == "E01.mkv"
        )
        pinned = _selector_definition(titles["Season 2"], (video.id,))
        apply_manual_split(session, titles["Season 2"].collection.id, [pinned])
        session.commit()
        video_id = video.id
        physical_title_id = titles["Season 1"].id
        pinned_title_id = titles["Season 2"].id

    with sessions() as session:
        pinned_title = session.get(CatalogTitle, pinned_title_id)
        apply_manual_split(
            session,
            pinned_title.catalog_collection_id,
            [replace(_selector_definition(pinned_title), video_ids=())],
        )
        video = session.get(Video, video_id)
        assert video.catalog_title_id == physical_title_id
        assert video.manual_split_rule_videos == []
        session.commit()

    with sessions() as session:
        plan = build_hierarchy_rebuild_plan(session)
        assignment = next(
            item for item in plan.video_assignments if item.video_id == video_id
        )
        assert assignment.changed is False
        assert assignment.target_title_path == "Anime/Show/Season 1"
        scan_library(session, library)

    with sessions() as session:
        assert session.get(Video, video_id).catalog_title_id == physical_title_id
    engine.dispose()


def test_b2_complete_manual_snapshot_protects_assignment_after_selector_removal(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'b2-manual-snapshot.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, _physical, pinned, _other, video = _manual_split_collection(session)
        _pin_video(session, collection, pinned, video)
        activate_manual_hierarchy_snapshot(
            pinned,
            part_type="season",
            season_number=2,
            part_number=None,
            season_label="S2",
            sort_order=2,
            verified_at=utc_now(),
        )
        apply_manual_split(
            session,
            collection.id,
            [_selector_definition(pinned)],
        )

        assert video.catalog_title_id == pinned.id
        assert video.manual_split_rule_videos == []
        assignment = next(
            item for item in build_hierarchy_rebuild_plan(session).video_assignments
            if item.video_id == video.id
        )
        assert assignment.changed is False
        assert assignment.target_title_path == pinned.relative_root_path
    engine.dispose()


def test_b2_other_explicit_selector_remains_authoritative(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'b2-other-explicit.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, _physical, pinned, other, video = _manual_split_collection(session)
        _pin_video(session, collection, pinned, video)
        apply_manual_split(
            session,
            collection.id,
            [
                _selector_definition(pinned),
                _selector_definition(other, (video.id,)),
            ],
        )

        assert video.catalog_title_id == other.id
        assert [
            link.catalog_title_id for link in video.manual_split_rule_videos
        ] == [other.id]
        decision = next(
            item for item in evaluate_persisted_manual_split(collection).decisions
            if item.video.id == video.id
        )
        assert decision.kind == ManualSplitDecisionKind.UNIQUE
        assert decision.target_catalog_title is other
    engine.dispose()


def test_b2_manual_collection_grouping_survives_released_assignment(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'b2-grouping.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        target, physical, pinned, _other, video = _manual_split_collection(session)
        target.relative_root_path = "@manual/show"
        source = CatalogCollection(
            local_title="Show source",
            normalized_local_title="show source",
            relative_root_path="Anime/Show",
        )
        session.add(source)
        session.flush()
        record_manual_collection_merge(session, target, [physical.id])
        _pin_video(session, target, pinned, video)

        apply_manual_split(
            session,
            target.id,
            [_selector_definition(pinned)],
        )

        assert video.catalog_title_id == physical.id
        assert video.catalog_collection_id == target.id
        assert physical.catalog_collection_id == target.id
    engine.dispose()


@pytest.mark.parametrize("selector", ["range", "pattern"])
def test_b2_collection_scope_selector_remains_authoritative(tmp_path, selector):
    engine = make_engine(f"sqlite:///{tmp_path / f'b2-{selector}.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, _physical, pinned, other, video = _manual_split_collection(session)
        _pin_video(session, collection, pinned, video)
        scoped = (
            _selector_definition(other, episode_start=1, episode_end=1)
            if selector == "range"
            else _selector_definition(other, filename_pattern=r"E01\.mkv$")
        )
        apply_manual_split(
            session,
            collection.id,
            [_selector_definition(pinned), scoped],
        )

        assert video.catalog_title_id == other.id
        assert video.manual_split_rule_videos == []
        decision = next(
            item for item in evaluate_persisted_manual_split(collection).decisions
            if item.video.id == video.id
        )
        assert decision.kind == ManualSplitDecisionKind.UNIQUE
        assert decision.target_catalog_title is other
    engine.dispose()


def test_b2_reconciliation_failure_rolls_back_authority_and_assignment(
    tmp_path,
    monkeypatch,
):
    engine = make_engine(f"sqlite:///{tmp_path / 'b2-rollback.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions() as session:
        collection, _physical, pinned, _other, video = _manual_split_collection(session)
        _pin_video(session, collection, pinned, video)
        session.commit()
        collection_id = collection.id
        pinned_id = pinned.id
        video_id = video.id

    def fail_reconciliation(*_args, **_kwargs):
        raise RuntimeError("simulated released-assignment failure")

    monkeypatch.setattr(
        hierarchy_review,
        "_reconcile_released_manual_split_assignments",
        fail_reconciliation,
    )
    with sessions() as session:
        pinned = session.get(CatalogTitle, pinned_id)
        with pytest.raises(RuntimeError, match="simulated"):
            apply_manual_split(
                session,
                collection_id,
                [_selector_definition(pinned)],
            )
        session.rollback()

    with sessions() as session:
        video = session.get(Video, video_id)
        assert video.catalog_title_id == pinned_id
        assert [
            link.catalog_title_id for link in video.manual_split_rule_videos
        ] == [pinned_id]
    engine.dispose()


def test_b7_confirmation_finalizes_downstream_numbering_before_return(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'b7-confirm.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)

    with sessions() as session:
        collection, season_1, season_2 = _numbering_collection(session)
        refresh_collection_state(collection)
        downstream = season_2.videos[0]
        assert (
            downstream.season_episode_number,
            downstream.absolute_episode_number,
        ) == (3, None)
        confirm_anilist_candidate(
            session,
            season_1,
            "1",
            Provider({"1": _metadata("1", episodes=2)}),
        )
        assert (
            downstream.season_episode_number,
            downstream.absolute_episode_number,
        ) == (1, 3)
        session.commit()
        collection_id = collection.id
        downstream_id = downstream.id

    with sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        before = (
            session.get(Video, downstream_id).season_episode_number,
            session.get(Video, downstream_id).absolute_episode_number,
        )
        refresh_collection_state(collection)
        after = (
            session.get(Video, downstream_id).season_episode_number,
            session.get(Video, downstream_id).absolute_episode_number,
        )
        assert before == after == (1, 3)
    engine.dispose()


def test_b7_metadata_confirm_post_commits_finalized_numbering(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'b7-confirm-post.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    web_app.state.metadata_provider = Provider({
        "1": _metadata("1", episodes=2),
    })
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection, season_1, season_2 = _numbering_collection(session)
        refresh_collection_state(collection)
        session.commit()
        title_id = season_1.id
        downstream_id = season_2.videos[0].id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None)
        == "/catalog/{filter_name}/titles/{catalog_title_id}/metadata/confirm"
    )
    response = endpoint(
        "all",
        title_id,
        external_id="1",
        candidate_id=None,
        confirm_conflict=False,
        confirm_locked=False,
        q="",
        sort="",
        direction="",
        detail_sort="",
        detail_direction="",
    )
    assert response.status_code == 303

    with web_app.state.sessions() as session:
        downstream = session.get(Video, downstream_id)
        assert (
            downstream.season_episode_number,
            downstream.absolute_episode_number,
        ) == (1, 3)
        assert session.get(TitleMetadata, title_id).episode_count == 2


def test_b7_episode_count_update_finalizes_owning_collection(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'b7-update.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, season_1, season_2 = _numbering_collection(
            session,
            downstream_number=4,
        )
        refresh_collection_state(collection)
        provider = Provider({"1": _metadata("1", episodes=2)})
        confirm_anilist_candidate(session, season_1, "1", provider)
        downstream = season_2.videos[0]
        assert (
            downstream.season_episode_number,
            downstream.absolute_episode_number,
        ) == (2, 4)

        provider.records["1"] = _metadata("1", episodes=3)
        refresh_title_metadata(session, season_1, provider)

        assert session.get(TitleMetadata, season_1.id).episode_count == 3
        assert (
            downstream.season_episode_number,
            downstream.absolute_episode_number,
        ) == (1, 4)
    engine.dispose()


def test_b7_unlink_finalizes_numbering_without_provider_evidence(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'b7-unlink.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, season_1, season_2 = _numbering_collection(session)
        refresh_collection_state(collection)
        confirm_anilist_candidate(
            session,
            season_1,
            "1",
            Provider({"1": _metadata("1", episodes=2)}),
        )
        downstream = season_2.videos[0]
        assert (
            downstream.season_episode_number,
            downstream.absolute_episode_number,
        ) == (1, 3)

        unlink_title_metadata(session, season_1)

        assert session.get(TitleMetadata, season_1.id) is None
        assert (
            downstream.season_episode_number,
            downstream.absolute_episode_number,
        ) == (3, None)
    engine.dispose()


def test_b7_non_numbering_metadata_changes_skip_collection_finalization(
    tmp_path,
    monkeypatch,
):
    engine = make_engine(f"sqlite:///{tmp_path / 'b7-no-impact.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, season_1, season_2 = _numbering_collection(session)
        refresh_collection_state(collection)
        provider = Provider({
            "1": _metadata("1", episodes=2),
            "2": _metadata("2", episodes=2),
        })
        confirm_anilist_candidate(session, season_1, "1", provider)
        before = (
            season_2.videos[0].season_episode_number,
            season_2.videos[0].absolute_episode_number,
        )
        calls: list[tuple[int, ...]] = []

        def record_finalization(collections):
            calls.append(tuple(item.id for item in collections))

        monkeypatch.setattr(
            metadata_service,
            "finalize_hierarchy_write",
            record_finalization,
        )
        provider.records["1"] = replace(
            provider.records["1"],
            description="Changed synopsis",
        )
        refresh_title_metadata(session, season_1, provider)
        confirm_anilist_candidate(session, season_1, "2", provider)

        assert calls == []
        assert (
            season_2.videos[0].season_episode_number,
            season_2.videos[0].absolute_episode_number,
        ) == before
    engine.dispose()


def test_b7_finalization_preserves_manual_episode_authority(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'b7-manual-number.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, season_1, season_2 = _numbering_collection(session)
        downstream = season_2.videos[0]
        set_video_episode_override(downstream, 7)
        refresh_collection_state(collection)
        confirm_anilist_candidate(
            session,
            season_1,
            "1",
            Provider({"1": _metadata("1", episodes=2)}),
        )

        assert downstream.episode_number_manual_override == 7
        assert (
            downstream.season_episode_number,
            downstream.absolute_episode_number,
        ) == (5, 7)
    engine.dispose()


def test_b7_finalization_failure_rolls_back_metadata_numbering_and_status(
    tmp_path,
    monkeypatch,
):
    engine = make_engine(f"sqlite:///{tmp_path / 'b7-rollback.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions() as session:
        collection, season_1, season_2 = _numbering_collection(session)
        refresh_collection_state(collection)
        session.commit()
        title_id = season_1.id
        collection_id = collection.id
        downstream_id = season_2.videos[0].id
        before = (
            season_2.videos[0].season_episode_number,
            season_2.videos[0].absolute_episode_number,
            collection.hierarchy_status,
            collection.hierarchy_note,
        )

    canonical_finalizer = metadata_service.finalize_hierarchy_write

    def fail_after_finalization(collections):
        canonical_finalizer(collections)
        raise RuntimeError("simulated metadata finalization failure")

    monkeypatch.setattr(
        metadata_service,
        "finalize_hierarchy_write",
        fail_after_finalization,
    )
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        with pytest.raises(RuntimeError, match="simulated"):
            confirm_anilist_candidate(
                session,
                title,
                "1",
                Provider({"1": _metadata("1", episodes=2)}),
            )
        session.rollback()

    with sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        downstream = session.get(Video, downstream_id)
        assert session.get(TitleMetadata, title_id) is None
        assert list(session.scalars(select(ExternalTitleLink))) == []
        title = session.get(CatalogTitle, title_id)
        assert title.metadata_status == "unlinked"
        assert title.preferred_metadata_provider is None
        assert title.preferred_external_id is None
        assert (
            downstream.season_episode_number,
            downstream.absolute_episode_number,
            collection.hierarchy_status,
            collection.hierarchy_note,
        ) == before
    engine.dispose()
