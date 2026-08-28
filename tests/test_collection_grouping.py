import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.config import Settings
from app.hierarchy_evaluation import (
    HierarchyIssueCode, evaluate_collection_hierarchy, finalize_hierarchy_write,
)
from app.hierarchy_review import (
    PROBABLE_GROUPING_REVIEW_REASON, CollectionGroupingMetrics,
    collection_grouping_suggestions, create_main_collection,
    delete_empty_collection, delete_empty_collections, delete_empty_local_title,
    move_titles_to_collection, record_grouping_decision,
    record_manual_collection_merge,
    reevaluate_automatic_collection_hierarchy,
)
from app.migrations import migrate_schema
from app.main import create_app
from app.models import (
    CatalogCollection, CatalogTitle, CollectionGroupingDecision,
    ExternalTitleLink, ManualSplitRuleVideo, TitleMetadata, Video, utc_now,
)
from app.numbering import set_duplicate_group_primary
from app.scanner import scan_library
from starlette.requests import Request


PROBE_RESULT = {
    "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
    "audio": [], "subtitles": [],
}


def _sessions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine)


def _episode_title(
    collection: CatalogCollection,
    *,
    title_name: str,
    title_path: str,
    episodes: tuple[int, ...] = (1, 2),
    part_type: str = "title",
    season_number: int | None = None,
    part_number: int | None = None,
    season_label: str | None = None,
) -> CatalogTitle:
    title = CatalogTitle(
        collection=collection,
        local_title=title_name,
        normalized_local_title=title_name.casefold(),
        relative_root_path=title_path,
        part_type=part_type,
        season_number=season_number,
        part_number=part_number,
        season_label=season_label,
    )
    for episode in episodes:
        Video(
            catalog_title=title,
            catalog_collection=collection,
            relative_path=f"{title_path}/E{episode:02}.mkv",
            root_folder="Anime",
            filename=f"E{episode:02}.mkv",
            size=episode,
            mtime_ns=episode,
        )
    return title


@pytest.mark.parametrize(("folders", "expected_types"), [
    (("Season 1", "Season 2"), {"season"}),
    (("Season 1", "OVA"), {"season", "ova"}),
    (("Season 1", "NC"), {"season", "bonus"}),
    (("Season 1", "Specials"), {"season", "special"}),
])
def test_scanner_groups_safe_child_parts_into_one_collection(
    tmp_path: Path, monkeypatch, folders, expected_types,
):
    for folder in folders:
        path = tmp_path / "Anime" / "Anime XYZ" / folder / "E01.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    _, sessions = _sessions()

    with sessions() as session:
        scan_library(session, tmp_path)
        collections = list(session.scalars(select(CatalogCollection)))
        titles = list(session.scalars(select(CatalogTitle)))

        assert len(collections) == 1
        assert collections[0].relative_root_path == "Anime/Anime XYZ"
        assert len(titles) == 2
        assert {title.part_type for title in titles} == expected_types


def test_peter_grill_pattern_keeps_different_season_metadata_in_one_collection(
    tmp_path: Path, monkeypatch,
):
    root = tmp_path / "Anime" / "Peter Grill To Kenja No Jikan"
    for folder in ("season 1 L20", "season 2 P22"):
        path = root / folder / "E01.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    _, sessions = _sessions()

    with sessions() as session:
        scan_library(session, tmp_path)
        titles = list(session.scalars(select(CatalogTitle).order_by(
            CatalogTitle.season_number
        )))
        session.add_all([
            TitleMetadata(
                catalog_title_id=titles[0].id,
                display_title="Peter Grill To Kenja No Jikan",
                title_romaji="Peter Grill to Kenja no Jikan",
            ),
            TitleMetadata(
                catalog_title_id=titles[1].id,
                display_title="Peter Grill To Kenja No Jikan - Super Extra",
                title_romaji="Peter Grill to Kenja no Jikan: Super Extra",
            ),
        ])
        session.commit()

        scan_library(session, tmp_path)

        collections = list(session.scalars(select(CatalogCollection)))
        titles = list(session.scalars(select(CatalogTitle).order_by(
            CatalogTitle.season_number
        )))
        assert len(collections) == 1
        assert collections[0].local_title == "Peter Grill To Kenja No Jikan"
        assert [(title.part_type, title.season_number) for title in titles] == [
            ("season", 1), ("season", 2),
        ]
        assert {title.catalog_collection_id for title in titles} == {collections[0].id}
        assert titles[1].metadata_record.title_romaji == (
            "Peter Grill to Kenja no Jikan: Super Extra"
        )


def test_kobayashi_pattern_groups_season_two_shorts_with_seasons(
    tmp_path: Path, monkeypatch,
):
    root = tmp_path / "Anime" / "Kobayashi-san Chi no Maid Dragon"
    for folder in ("Season 1 (Z17)", "Season 2 (L21)", "Season 2 Shorts (L21)"):
        filename = "Short 01.mkv" if "Shorts" in folder else "E01.mkv"
        path = root / folder / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    _, sessions = _sessions()

    with sessions() as session:
        scan_library(session, tmp_path)

        collections = list(session.scalars(select(CatalogCollection)))
        titles = list(session.scalars(select(CatalogTitle)))
        shorts = next(title for title in titles if "Shorts" in title.local_title)
        assert len(collections) == 1
        assert collections[0].local_title == "Kobayashi-san Chi no Maid Dragon"
        assert len(titles) == 3
        assert {title.catalog_collection_id for title in titles} == {collections[0].id}
        assert (shorts.part_type, shorts.season_number, shorts.season_label) == (
            "bonus", 2, "S2",
        )
        assert {video.file_type for video in shorts.videos} == {"other"}
        assert all(video.season_episode_number is None for video in shorts.videos)


def test_manual_reassignment_of_scoped_supplementary_title_remains_authoritative(
    tmp_path: Path, monkeypatch,
):
    path = tmp_path / "Anime" / "Show" / "Season 2 Shorts" / "Short 01.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    _, sessions = _sessions()

    with sessions() as session:
        scan_library(session, tmp_path)
        title = session.scalar(select(CatalogTitle))
        target = CatalogCollection(
            local_title="Manual target", normalized_local_title="manual target",
            relative_root_path="@manual/scoped-shorts",
        )
        session.add(target)
        session.flush()
        title.collection = target
        title.part_type_manual = title.part_type
        title.season_number_manual = title.season_number
        title.part_number_manual = title.part_number
        title.season_label_manual = title.season_label
        title.hierarchy_manual_override = True
        title.hierarchy_verified_at = utc_now()
        for video in title.videos:
            video.catalog_collection = target
        session.commit()
        target_id = target.id

        scan_library(session, tmp_path)

        stored = session.get(CatalogTitle, title.id)
        assert stored.catalog_collection_id == target_id
        assert stored.hierarchy_manual_override is True
        assert {video.catalog_collection_id for video in stored.videos} == {target_id}


def test_same_metadata_title_does_not_merge_unrelated_physical_anime_roots(
    tmp_path: Path, monkeypatch,
):
    for folder in ("Unrelated Alpha", "Unrelated Beta"):
        path = tmp_path / "Anime" / folder / "E01.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    _, sessions = _sessions()

    with sessions() as session:
        scan_library(session, tmp_path)
        titles = list(session.scalars(select(CatalogTitle)))
        session.add_all([
            TitleMetadata(
                catalog_title_id=title.id, display_title="Same metadata title",
                title_romaji="Same metadata title",
            )
            for title in titles
        ])
        session.commit()

        scan_library(session, tmp_path)

        collections = list(session.scalars(select(CatalogCollection)))
        assert len(collections) == 2
        assert {collection.relative_root_path for collection in collections} == {
            "Anime/Unrelated Alpha", "Anime/Unrelated Beta",
        }


def test_scanner_groups_film_and_cmpv_bonus(tmp_path: Path, monkeypatch):
    movie = tmp_path / "Anime" / "Tenki no Ko (FILM)" / "Tenki no Ko.mkv"
    bonus = tmp_path / "Anime" / "Tenki no Ko (FILM)" / "CM&PV" / "Trailer.mkv"
    movie.parent.mkdir(parents=True)
    bonus.parent.mkdir(parents=True)
    movie.write_bytes(b"movie")
    bonus.write_bytes(b"bonus")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    _, sessions = _sessions()

    with sessions() as session:
        scan_library(session, tmp_path)
        collection = session.scalar(select(CatalogCollection))

        assert collection.relative_root_path == "Anime/Tenki no Ko (FILM)"
        assert {title.part_type for title in collection.titles} == {"film", "bonus"}


def test_scanner_groups_related_named_child_but_requires_review(
    tmp_path: Path, monkeypatch,
):
    path = (
        tmp_path / "Anime" / "High School DxD"
        / "High School DxD Born (J15)" / "E01.mkv"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    _, sessions = _sessions()

    with sessions() as session:
        scan_library(session, tmp_path)
        collection = session.scalar(select(CatalogCollection))

        assert collection.relative_root_path == "Anime/High School DxD"
        assert [title.local_title for title in collection.titles] == [
            "High School DxD Born (J15)"
        ]
        assert collection.hierarchy_status == "review_required"
        assert collection.hierarchy_note == PROBABLE_GROUPING_REVIEW_REASON


def test_manual_collection_move_preserves_title_video_metadata_and_scan(
    tmp_path: Path, monkeypatch,
):
    paths = [
        tmp_path / "Anime" / "High School DxD Born" / f"Episode {number:02}.mkv"
        for number in (1, 2)
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine, sessions = _sessions()

    with sessions() as session:
        scan_library(session, tmp_path)
        source = session.scalar(select(CatalogCollection))
        title = source.titles[0]
        videos = sorted(title.videos, key=lambda item: item.filename)
        videos[0].episode_number_manual_override = 7
        videos[0].content_type_manual = "other"
        title.season_number_manual = 3
        title.season_label_manual = "S3"
        title.part_type_manual = "season"
        title.hierarchy_manual_override = True
        title.hierarchy_verified_at = utc_now()
        session.add(TitleMetadata(
            catalog_title_id=title.id, display_title="High School DxD Born",
            metadata_provider="anilist", metadata_external_id="123",
        ))
        session.add(ExternalTitleLink(
            catalog_title_id=title.id, provider="anilist", external_id="123",
            match_method="manual_search", is_primary=True, is_manual=True,
        ))
        target = CatalogCollection(
            local_title="High School DxD", normalized_local_title="high school dxd",
            relative_root_path="@manual/high-school-dxd",
        )
        session.add(target)
        session.flush()
        original = {
            video.id: (
                video.catalog_title_id, video.filename, video.relative_path,
                video.episode_number_manual_override, video.content_type_manual,
            )
            for video in videos
        }

        move_titles_to_collection(session, target.id, [title.id])
        evaluated = evaluate_collection_hierarchy(target, list(target.videos))
        assert (target.hierarchy_status, target.hierarchy_note) == (
            evaluated.status,
            evaluated.primary_note,
        )
        session.commit()
        source_id, target_id, title_id = source.id, target.id, title.id

    migrate_schema(engine)

    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        assert title.catalog_collection_id == target_id
        assert title.metadata_record.metadata_external_id == "123"
        assert title.external_links[0].external_id == "123"
        assert title.season_number_manual == 3
        assert title.season_label_manual == "S3"
        assert title.part_type_manual == "season"
        assert {
            video.id: (
                video.catalog_title_id, video.filename, video.relative_path,
                video.episode_number_manual_override, video.content_type_manual,
            )
            for video in title.videos
        } == original
        assert session.get(CatalogCollection, source_id) is not None
        assert session.get(CatalogCollection, source_id).titles == []

        scan_library(session, tmp_path)
        title = session.get(CatalogTitle, title_id)
        assert title.catalog_collection_id == target_id
        assert all(video.catalog_collection_id == target_id for video in title.videos)
        assert session.scalar(select(func.count()).select_from(Video)) == 2


def test_move_reinfers_automatic_singleton_s1_in_target_context():
    _, sessions = _sessions()
    with sessions() as session:
        target = CatalogCollection(
            local_title="Target", normalized_local_title="target",
            relative_root_path="Anime/Target",
        )
        source = CatalogCollection(
            local_title="Fragment", normalized_local_title="fragment",
            relative_root_path="Anime/Fragment",
        )
        target_title = _episode_title(
            target, title_name="Target", title_path="Anime/Target",
        )
        moved = _episode_title(
            source, title_name="Fragment", title_path="Anime/Fragment",
        )
        session.add_all([target, source])
        session.flush()
        finalize_hierarchy_write([target, source])
        before_numbering = {
            video.id: (
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.external_episode_number,
            )
            for video in moved.videos
        }
        assert (target_title.part_type, target_title.season_number) == ("season", 1)
        assert (moved.part_type, moved.season_number) == ("season", 1)

        move_titles_to_collection(session, target.id, [moved.id])

        assert (target_title.part_type, target_title.season_number) == ("season", 1)
        assert (
            moved.part_type, moved.season_number, moved.part_number,
            moved.season_label,
        ) == ("title", None, None, None)
        assert moved.hierarchy_manual_override is False
        assert moved.hierarchy_verified_at is None
        evaluation = evaluate_collection_hierarchy(target, list(target.videos))
        assert target.hierarchy_status == "review_required"
        assert HierarchyIssueCode.AMBIGUOUS_SPLIT_SEASON not in {
            issue.code for issue in evaluation.issues
        }
        assert HierarchyIssueCode.GENERIC_STRUCTURAL_TYPE in {
            issue.code for issue in evaluation.issues
        }
        assert before_numbering == {
            video.id: (
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.external_episode_number,
            )
            for video in moved.videos
        }
        assert source.titles == []
        source_evaluation = evaluate_collection_hierarchy(source, list(source.videos))
        assert (source.hierarchy_status, source.hierarchy_note) == (
            source_evaluation.status, source_evaluation.primary_note,
        )


def test_move_reinfers_supported_explicit_season_two_signal():
    _, sessions = _sessions()
    with sessions() as session:
        source = CatalogCollection(
            local_title="Fragment", normalized_local_title="fragment",
            relative_root_path="Anime/Fragment",
        )
        target = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        moved = _episode_title(
            source,
            title_name="Season 2",
            title_path="Anime/Fragment/Season 2",
            part_type="season",
            season_number=2,
            season_label="S2",
        )
        session.add_all([source, target])
        session.flush()

        move_titles_to_collection(session, target.id, [moved.id])

        assert (
            moved.part_type, moved.season_number, moved.part_number,
            moved.season_label,
        ) == ("season", 2, None, "S2")
        assert moved.hierarchy_manual_override is False


@pytest.mark.parametrize(
    ("season_number", "season_label"),
    [(2, "S2"), (None, None)],
)
def test_move_preserves_complete_manual_season_authority_including_null(
    season_number, season_label,
):
    _, sessions = _sessions()
    timestamp = utc_now()
    with sessions() as session:
        source = CatalogCollection(
            local_title="Fragment", normalized_local_title="fragment",
            relative_root_path="Anime/Fragment",
        )
        target = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        moved = _episode_title(
            source, title_name="Fragment", title_path="Anime/Fragment",
        )
        moved.hierarchy_manual_override = True
        moved.part_type_manual = "season"
        moved.season_number_manual = season_number
        moved.part_number_manual = None
        moved.season_label_manual = season_label
        moved.hierarchy_verified_at = timestamp
        session.add_all([source, target])
        session.flush()
        finalize_hierarchy_write([source])
        automatic_before = (
            moved.part_type, moved.season_number, moved.part_number,
            moved.season_label,
        )

        move_titles_to_collection(session, target.id, [moved.id])

        assert (
            moved.part_type_manual,
            moved.season_number_manual,
            moved.part_number_manual,
            moved.season_label_manual,
            moved.hierarchy_manual_override,
            moved.hierarchy_verified_at,
        ) == ("season", season_number, None, season_label, True, timestamp)
        assert moved.effective_season_number == season_number
        assert (
            moved.part_type, moved.season_number, moved.part_number,
            moved.season_label,
        ) == automatic_before


def test_three_singleton_s1_moves_do_not_create_three_stale_seasons():
    _, sessions = _sessions()
    with sessions() as session:
        collections = [
            CatalogCollection(
                local_title=name,
                normalized_local_title=name.casefold(),
                relative_root_path=f"Anime/{name}",
            )
            for name in ("Main", "Fragment B", "Fragment C")
        ]
        titles = [
            _episode_title(
                collection,
                title_name=collection.local_title,
                title_path=collection.relative_root_path,
            )
            for collection in collections
        ]
        session.add_all(collections)
        session.flush()
        finalize_hierarchy_write(collections)
        assert [title.season_number for title in titles] == [1, 1, 1]

        move_titles_to_collection(
            session, collections[0].id, [titles[1].id, titles[2].id],
        )

        assert [title.season_number for title in titles] == [1, None, None]
        evaluation = evaluate_collection_hierarchy(
            collections[0], list(collections[0].videos),
        )
        assert HierarchyIssueCode.AMBIGUOUS_SPLIT_SEASON not in {
            issue.code for issue in evaluation.issues
        }


def test_explicit_duplicate_season_evidence_remains_split_season_blocker():
    _, sessions = _sessions()
    with sessions() as session:
        target = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="@manual/show",
        )
        sources = []
        titles = []
        for suffix in ("A", "B"):
            source = CatalogCollection(
                local_title=f"Fragment {suffix}",
                normalized_local_title=f"fragment {suffix.casefold()}",
                relative_root_path=f"Anime/Fragment {suffix}",
            )
            title = _episode_title(
                source,
                title_name="Season 1",
                title_path=f"Anime/Fragment {suffix}/Season 1",
                part_type="season",
                season_number=1,
                season_label="S1",
            )
            sources.append(source)
            titles.append(title)
        session.add_all([target, *sources])
        session.flush()

        move_titles_to_collection(session, target.id, [title.id for title in titles])

        assert [title.season_number for title in titles] == [1, 1]
        evaluation = evaluate_collection_hierarchy(target, list(target.videos))
        assert target.hierarchy_status == "review_required"
        assert HierarchyIssueCode.AMBIGUOUS_SPLIT_SEASON in {
            issue.code for issue in evaluation.issues
        }


def test_manual_split_parts_and_numbering_survive_collection_move():
    _, sessions = _sessions()
    timestamp = utc_now()
    with sessions() as session:
        source = CatalogCollection(
            local_title="Split", normalized_local_title="split",
            relative_root_path="Anime/Split",
        )
        target = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="@manual/show",
        )
        first = _episode_title(
            source, title_name="First half", title_path="Anime/Split/First half",
            episodes=(1, 2),
        )
        second = _episode_title(
            source, title_name="Second half", title_path="Anime/Split/Second half",
            episodes=(3, 4),
        )
        for title, part_number in ((first, 1), (second, 2)):
            title.hierarchy_manual_override = True
            title.part_type_manual = "season"
            title.season_number_manual = 1
            title.part_number_manual = part_number
            title.season_label_manual = "S1"
            title.hierarchy_verified_at = timestamp
        session.add_all([source, target])
        session.flush()
        finalize_hierarchy_write([source])
        numbering = {
            video.id: (
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.external_episode_number,
            )
            for title in (first, second) for video in title.videos
        }

        move_titles_to_collection(session, target.id, [first.id, second.id])

        assert [title.effective_part_number for title in (first, second)] == [1, 2]
        assert numbering == {
            video.id: (
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.external_episode_number,
            )
            for title in (first, second) for video in title.videos
        }
        assert HierarchyIssueCode.AMBIGUOUS_SPLIT_SEASON not in {
            issue.code
            for issue in evaluate_collection_hierarchy(
                target, list(target.videos),
            ).issues
        }


def test_collection_move_rolls_back_membership_and_invalidation_atomically(
    monkeypatch,
):
    _, sessions = _sessions()
    with sessions() as session:
        source = CatalogCollection(
            local_title="Fragment", normalized_local_title="fragment",
            relative_root_path="Anime/Fragment",
        )
        target = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        moved = _episode_title(
            source, title_name="Fragment", title_path="Anime/Fragment",
        )
        session.add_all([source, target])
        session.commit()
        finalize_hierarchy_write([source])
        session.commit()
        moved_id, source_id = moved.id, source.id
        video_ids = [video.id for video in moved.videos]

        def fail_finalization(*_args, **_kwargs):
            raise ValueError("simulated finalizer failure")

        monkeypatch.setattr(
            "app.hierarchy_review.finalize_hierarchy_write", fail_finalization,
        )
        with pytest.raises(ValueError, match="simulated finalizer failure"):
            move_titles_to_collection(session, target.id, [moved.id])
        session.rollback()
        session.expire_all()

        stored = session.get(CatalogTitle, moved_id)
        assert stored.catalog_collection_id == source_id
        assert (stored.part_type, stored.season_number, stored.season_label) == (
            "season", 1, "S1",
        )
        assert {
            session.get(Video, video_id).catalog_collection_id
            for video_id in video_ids
        } == {source_id}


def test_explicit_collection_context_reevaluation_repairs_all_stale_automatic_s1():
    _, sessions = _sessions()
    timestamp = utc_now()
    with sessions() as session:
        collection = CatalogCollection(
            local_title="Main", normalized_local_title="main",
            relative_root_path="Anime/Main",
        )
        first = _episode_title(
            collection, title_name="Fragment A", title_path="Anime/Fragment A",
            part_type="season", season_number=1, season_label="S1",
        )
        second = _episode_title(
            collection, title_name="Fragment B", title_path="Anime/Fragment B",
            part_type="season", season_number=1, season_label="S1",
        )
        manual = _episode_title(
            collection, title_name="Main", title_path="Anime/Main",
            part_type="season", season_number=1, season_label="S1",
        )
        manual.hierarchy_manual_override = True
        manual.part_type_manual = "season"
        manual.season_number_manual = 1
        manual.part_number_manual = None
        manual.season_label_manual = "S1"
        manual.hierarchy_verified_at = timestamp
        duplicate = Video(
            catalog_title=first, catalog_collection=collection,
            relative_path="Anime/Fragment A/E01 copy.mkv", root_folder="Anime",
            filename="E01 copy.mkv", size=100, mtime_ns=100,
        )
        second.videos[-1].duplicate_status_manual = "suspected"
        session.add(collection)
        session.flush()
        finalize_hierarchy_write([collection])
        set_duplicate_group_primary([first.videos[0], duplicate], first.videos[0])
        session.add_all([
            TitleMetadata(
                catalog_title_id=first.id,
                display_title="Fragment A metadata",
                metadata_provider="anilist",
                metadata_external_id="1001",
            ),
            ExternalTitleLink(
                catalog_title_id=first.id,
                provider="anilist", external_id="1001",
                match_method="manual_search", is_primary=True, is_manual=True,
            ),
        ])
        session.flush()
        session.expire(first, ["metadata_record", "external_links"])
        title_ids = tuple(title.id for title in (first, second, manual))
        membership = {
            video.id: (video.catalog_title_id, video.catalog_collection_id)
            for video in collection.videos
        }
        numbering = {
            video.id: (
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.external_episode_number,
                video.episode_number_source,
                video.episode_number_confidence,
            )
            for video in collection.videos
        }
        duplicate_state = {
            video.id: (
                video.duplicate_of_video_id,
                video.duplicate_primary_missing,
                video.duplicate_status_manual,
            )
            for video in collection.videos
        }

        reevaluate_automatic_collection_hierarchy(session, collection.id)

        assert tuple(title.id for title in (first, second, manual)) == title_ids
        assert (
            first.part_type, first.season_number, first.part_number,
            first.season_label,
        ) == ("title", None, None, None)
        assert (
            second.part_type, second.season_number, second.part_number,
            second.season_label,
        ) == ("title", None, None, None)
        assert (
            manual.part_type_manual,
            manual.season_number_manual,
            manual.part_number_manual,
            manual.season_label_manual,
            manual.hierarchy_manual_override,
            manual.hierarchy_verified_at,
        ) == ("season", 1, None, "S1", True, timestamp)
        assert membership == {
            video.id: (video.catalog_title_id, video.catalog_collection_id)
            for video in collection.videos
        }
        assert numbering == {
            video.id: (
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.external_episode_number,
                video.episode_number_source,
                video.episode_number_confidence,
            )
            for video in collection.videos
        }
        assert duplicate_state == {
            video.id: (
                video.duplicate_of_video_id,
                video.duplicate_primary_missing,
                video.duplicate_status_manual,
            )
            for video in collection.videos
        }
        assert first.metadata_record.metadata_external_id == "1001"
        assert first.external_links[0].external_id == "1001"
        evaluation = evaluate_collection_hierarchy(collection, list(collection.videos))
        assert HierarchyIssueCode.AMBIGUOUS_SPLIT_SEASON not in {
            issue.code for issue in evaluation.issues
        }
        assert collection.hierarchy_status == "review_required"


def test_explicit_reevaluation_rederives_raw_s2_and_preserves_manual_s2_and_null():
    _, sessions = _sessions()
    timestamp = utc_now()
    with sessions() as session:
        collection = CatalogCollection(
            local_title="Current", normalized_local_title="current",
            relative_root_path="@manual/current",
        )
        raw_s2 = _episode_title(
            collection, title_name="Season 2",
            title_path="Anime/Fragment/Season 2",
            part_type="season", season_number=1, season_label="S1",
        )
        manual_s2 = _episode_title(
            collection, title_name="Manual S2",
            title_path="Anime/Manual S2",
            part_type="season", season_number=1, season_label="S1",
        )
        manual_null = _episode_title(
            collection, title_name="Manual unknown season",
            title_path="Anime/Manual unknown season",
            part_type="season", season_number=1, season_label="S1",
        )
        for title, season_number, label in (
            (manual_s2, 2, "S2"),
            (manual_null, None, None),
        ):
            title.hierarchy_manual_override = True
            title.part_type_manual = "season"
            title.season_number_manual = season_number
            title.part_number_manual = None
            title.season_label_manual = label
            title.hierarchy_verified_at = timestamp
        session.add(collection)
        session.flush()
        automatic_caches = {
            title.id: (
                title.part_type, title.season_number, title.part_number,
                title.season_label,
            )
            for title in (manual_s2, manual_null)
        }

        reevaluate_automatic_collection_hierarchy(session, collection.id)

        assert (
            raw_s2.part_type, raw_s2.season_number, raw_s2.part_number,
            raw_s2.season_label,
        ) == ("season", 2, None, "S2")
        assert (manual_s2.effective_part_type, manual_s2.effective_season_number) == (
            "season", 2,
        )
        assert (
            manual_null.effective_part_type,
            manual_null.effective_season_number,
            manual_null.effective_season_label,
        ) == ("season", None, None)
        assert {
            title.id: (
                title.part_type, title.season_number, title.part_number,
                title.season_label,
            )
            for title in (manual_s2, manual_null)
        } == automatic_caches
        assert all(
            title.hierarchy_verified_at == timestamp
            for title in (manual_s2, manual_null)
        )


def test_explicit_collection_context_reevaluation_rolls_back_atomically(monkeypatch):
    _, sessions = _sessions()
    with sessions() as session:
        collection = CatalogCollection(
            local_title="Current", normalized_local_title="current",
            relative_root_path="@manual/current",
        )
        stale = _episode_title(
            collection, title_name="Fragment", title_path="Anime/Fragment",
            part_type="season", season_number=1, season_label="S1",
        )
        session.add(collection)
        session.commit()
        collection_id, title_id = collection.id, stale.id

        def fail_finalization(*_args, **_kwargs):
            raise ValueError("simulated reevaluation failure")

        monkeypatch.setattr(
            "app.hierarchy_review.finalize_hierarchy_write", fail_finalization,
        )
        with pytest.raises(ValueError, match="simulated reevaluation failure"):
            reevaluate_automatic_collection_hierarchy(session, collection_id)
        session.rollback()
        session.expire_all()

        stored = session.get(CatalogTitle, title_id)
        assert stored.catalog_collection_id == collection_id
        assert (
            stored.part_type, stored.season_number, stored.part_number,
            stored.season_label,
        ) == ("season", 1, None, "S1")


def test_move_without_manual_snapshot_does_not_create_false_authority():
    _, sessions = _sessions()
    with sessions() as session:
        source = CatalogCollection(
            local_title="Source", normalized_local_title="source",
            relative_root_path="Anime/Source",
        )
        target = CatalogCollection(
            local_title="Target", normalized_local_title="target",
            relative_root_path="@manual/target",
        )
        title = CatalogTitle(
            collection=source, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Source/Season 1",
            part_type="season", season_number=1, season_label="S1",
        )
        video = Video(
            relative_path="Anime/Source/Season 1/E01.mkv", root_folder="Anime",
            filename="E01.mkv", size=1, mtime_ns=1,
            catalog_collection=source, catalog_title=title,
        )
        session.add_all([target, video])
        session.flush()

        move_titles_to_collection(session, target.id, [title.id])

        assert title.hierarchy_manual_override is False
        assert title.hierarchy_verified_at is None
        assert video.catalog_collection_id == target.id
        assert title.catalog_collection_id == target.id
        assert target.hierarchy_status == "automatic"


def test_move_preserves_historical_incomplete_snapshot_for_structured_review():
    _, sessions = _sessions()
    timestamp = utc_now()
    with sessions() as session:
        source = CatalogCollection(
            local_title="Source", normalized_local_title="source",
            relative_root_path="Anime/Source",
        )
        target = CatalogCollection(
            local_title="Target", normalized_local_title="target",
            relative_root_path="@manual/target",
        )
        title = CatalogTitle(
            collection=source, local_title="Part",
            normalized_local_title="part", relative_root_path="Anime/Source/Part",
            part_type="part", season_number=1, part_number=2,
            part_type_manual="part", season_number_manual=1,
            part_number_manual=None, hierarchy_manual_override=True,
            hierarchy_verified_at=timestamp,
        )
        video = Video(
            relative_path="Anime/Source/Part/E01.mkv", root_folder="Anime",
            filename="E01.mkv", size=1, mtime_ns=1,
            catalog_collection=source, catalog_title=title,
        )
        session.add_all([target, video])
        session.flush()

        move_titles_to_collection(session, target.id, [title.id])

        assert title.part_type_manual == "part"
        assert title.part_number_manual is None
        assert title.hierarchy_manual_override is True
        assert title.hierarchy_verified_at == timestamp
        assert title.effective_part_number == 2
        assert target.hierarchy_status == "review_required"
        assert target.hierarchy_note.startswith(
            "Historické ruční zařazení není úplné."
        )
        assert video.catalog_collection_id == target.id


def test_move_keeps_conflicting_selector_authority_together_and_unassigned():
    _, sessions = _sessions()
    with sessions() as session:
        source = CatalogCollection(
            local_title="Source", normalized_local_title="source",
            relative_root_path="Anime/Source",
        )
        target = CatalogCollection(
            local_title="Target", normalized_local_title="target",
            relative_root_path="@manual/target",
        )
        first = CatalogTitle(
            collection=source, local_title="A", normalized_local_title="a",
            relative_root_path="Anime/Source/.catalog-part-1",
            part_type="season", season_number=1,
        )
        second = CatalogTitle(
            collection=source, local_title="B", normalized_local_title="b",
            relative_root_path="Anime/Source/.catalog-part-2",
            part_type="season", season_number=2,
        )
        video = Video(
            relative_path="Anime/Source/E01.mkv", root_folder="Anime",
            filename="E01.mkv", size=1, mtime_ns=1,
            catalog_collection=source,
        )
        session.add_all([
            target,
            video,
            ManualSplitRuleVideo(catalog_title=first, video=video),
            ManualSplitRuleVideo(catalog_title=second, video=video),
        ])
        session.commit()

        with pytest.raises(ValueError, match="selector authority"):
            move_titles_to_collection(session, target.id, [first.id])
        session.rollback()

    with sessions() as session:
        source = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Anime/Source"
        ))
        target = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "@manual/target"
        ))
        titles = list(source.titles)
        video = source.videos[0]
        move_titles_to_collection(session, target.id, [title.id for title in titles])

        assert video.catalog_title_id is None
        assert video.catalog_collection_id == target.id
        assert {link.catalog_title_id for link in video.manual_split_rule_videos} == {
            title.id for title in titles
        }
        assert all(title.catalog_collection_id == target.id for title in titles)
        assert target.hierarchy_status == "conflict"


def test_deleted_fragment_collection_is_not_recreated_by_following_scan(
    tmp_path: Path, monkeypatch,
):
    season_path = tmp_path / "Anime" / "High School DxD" / "Season 1" / "E01.mkv"
    born_path = (
        tmp_path / "Anime" / "High School DxD"
        / "High School DxD Born" / "E01.mkv"
    )
    for path in (season_path, born_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    _, sessions = _sessions()

    with sessions() as session:
        main = CatalogCollection(
            local_title="High School DxD", normalized_local_title="high school dxd",
            relative_root_path="Anime/High School DxD",
        )
        season = CatalogTitle(
            collection=main, local_title="Season 1", normalized_local_title="season 1",
            relative_root_path="Anime/High School DxD/Season 1",
            part_type="season", season_number=1,
        )
        Video(
            catalog_title=season, catalog_collection=main,
            relative_path="Anime/High School DxD/Season 1/E01.mkv",
            root_folder="Anime", filename="E01.mkv", size=5, mtime_ns=1,
        )
        fragment = CatalogCollection(
            local_title="High School DxD Born",
            normalized_local_title="high school dxd born",
            relative_root_path="Anime/High School DxD/High School DxD Born",
        )
        born = CatalogTitle(
            collection=fragment, local_title="High School DxD Born",
            normalized_local_title="high school dxd born",
            relative_root_path=fragment.relative_root_path,
        )
        Video(
            catalog_title=born, catalog_collection=fragment,
            relative_path="Anime/High School DxD/High School DxD Born/E01.mkv",
            root_folder="Anime", filename="E01.mkv", size=5, mtime_ns=1,
        )
        session.add_all([main, fragment])
        session.commit()

        suggestion = collection_grouping_suggestions(session)[0]
        assert {item.id for item in suggestion.collections} == {main.id, fragment.id}
        record_grouping_decision(
            session, suggestion, "merged", target_collection=main,
            selected_title_ids=[born.id],
        )
        move_titles_to_collection(session, main.id, [born.id])
        fragment_id = fragment.id
        delete_empty_collection(session, fragment_id)
        session.commit()

        scan_library(session, tmp_path)

        assert session.get(CatalogCollection, fragment_id) is None
        assert session.scalar(select(func.count()).select_from(CatalogCollection)) == 1
        refreshed_main = session.get(CatalogCollection, main.id)
        assert {title.local_title for title in refreshed_main.titles} == {
            "Season 1", "High School DxD Born",
        }
        assert session.scalar(select(func.count()).select_from(Video)) == 2
        assert collection_grouping_suggestions(session) == []
        assert session.scalar(select(func.count()).select_from(
            CollectionGroupingDecision
        )) == 1


def _high_school_dxd_grouping_files(tmp_path: Path) -> None:
    paths = (
        tmp_path / "High School DxD (Z12-J18)" / "High School DxD (Z12)" / "E01.mkv",
        tmp_path / "High School DxD (Z12-J18)" / "High School DxD Born (J15)" / "E01.mkv",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")


def _populated_collection_paths(session: Session) -> set[str]:
    return {
        collection.relative_root_path
        for collection in session.scalars(select(CatalogCollection)).all()
        if collection.titles or collection.videos
    }


def test_manual_collection_merge_survives_startup_rescan_and_restart(
    tmp_path: Path, monkeypatch,
):
    _high_school_dxd_grouping_files(tmp_path)
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine(f"sqlite:///{tmp_path / 'grouping-restart.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, tmp_path)
        suggestion = collection_grouping_suggestions(session)[0]
        target = session.get(CatalogCollection, suggestion.target_collection_id)
        selected_title_ids = list(suggestion.title_ids)
        born = session.scalar(select(CatalogTitle).where(
            CatalogTitle.local_title == "High School DxD Born (J15)"
        ))
        born.hierarchy_manual_override = True
        born.part_type_manual = "season"
        born.season_number_manual = 3
        born.season_label_manual = "S3"
        born.sort_order_manual = 0
        born.hierarchy_verified_at = utc_now()
        authority = (
            born.part_type_manual, born.season_number_manual,
            born.season_label_manual, born.hierarchy_manual_override,
            born.hierarchy_verified_at.replace(tzinfo=None),
        )
        move_titles_to_collection(session, target.id, selected_title_ids)
        record_grouping_decision(
            session, suggestion, "merged", target_collection=target,
            selected_title_ids=selected_title_ids,
        )
        session.commit()
        target_path = target.relative_root_path
        born_id = born.id

    migrate_schema(engine)
    with Session(engine) as session:
        born = session.get(CatalogTitle, born_id)
        assert born.collection.relative_root_path == target_path
        assert _populated_collection_paths(session) == {target_path}
        assert (
            born.part_type_manual, born.season_number_manual,
            born.season_label_manual, born.hierarchy_manual_override,
            born.hierarchy_verified_at.replace(tzinfo=None),
        ) == authority
        decision = session.scalar(select(CollectionGroupingDecision))
        assert decision.decision == "merged"
        assert decision.target_collection_path == target_path
        assert set(json.loads(decision.selected_title_paths_json)) == {
            title.relative_root_path for title in session.scalars(select(CatalogTitle))
        }
        assert collection_grouping_suggestions(session) == []

        scan_library(session, tmp_path)

    migrate_schema(engine)
    with Session(engine) as session:
        born = session.get(CatalogTitle, born_id)
        assert born.collection.relative_root_path == target_path
        assert _populated_collection_paths(session) == {target_path}
        assert (
            born.part_type_manual, born.season_number_manual,
            born.season_label_manual, born.hierarchy_manual_override,
            born.hierarchy_verified_at.replace(tzinfo=None),
        ) == authority
        assert collection_grouping_suggestions(session) == []


def test_automatic_collection_move_survives_startup_scanner_and_is_idempotent(
    tmp_path: Path, monkeypatch,
):
    for name in ("Main", "Fragment"):
        for episode in (1, 2):
            path = tmp_path / "Anime" / name / f"E{episode:02}.mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine(f"sqlite:///{tmp_path / 'automatic-move.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, tmp_path)
        target = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Anime/Main"
        ))
        source = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Anime/Fragment"
        ))
        target_title = target.titles[0]
        moved = source.titles[0]
        assert (target_title.part_type, target_title.season_number) == ("season", 1)
        assert (moved.part_type, moved.season_number) == ("season", 1)
        moved_id = moved.id
        target_id = target.id
        membership = {
            video.id: (video.catalog_title_id, video.relative_path)
            for video in moved.videos
        }

        move_titles_to_collection(session, target.id, [moved.id])
        record_manual_collection_merge(session, target, [moved.id])
        session.commit()
        assert (
            moved.part_type, moved.season_number, moved.part_number,
            moved.season_label,
        ) == ("title", None, None, None)

    migrate_schema(engine)
    with Session(engine) as session:
        moved = session.get(CatalogTitle, moved_id)
        assert moved.catalog_collection_id == target_id
        assert (
            moved.part_type, moved.season_number, moved.part_number,
            moved.season_label,
        ) == ("title", None, None, None)
        assert {
            video.id: (video.catalog_title_id, video.relative_path)
            for video in moved.videos
        } == membership

        scan_library(session, tmp_path)
        moved = session.get(CatalogTitle, moved_id)
        assert moved.catalog_collection_id == target_id
        assert (
            moved.part_type, moved.season_number, moved.part_number,
            moved.season_label,
        ) == ("title", None, None, None)
        assert {
            video.id: (video.catalog_title_id, video.relative_path)
            for video in moved.videos
        } == membership

    with Session(engine) as session:
        before = {
            title.id: (
                title.catalog_collection_id,
                title.part_type,
                title.season_number,
                title.part_number,
                title.season_label,
                title.updated_at,
            )
            for title in session.scalars(select(CatalogTitle))
        }
    updates = []

    def record_update(_connection, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE CATALOG_TITLES"):
            updates.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record_update)
    try:
        migrate_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", record_update)

    with Session(engine) as session:
        after = {
            title.id: (
                title.catalog_collection_id,
                title.part_type,
                title.season_number,
                title.part_number,
                title.season_label,
                title.updated_at,
            )
            for title in session.scalars(select(CatalogTitle))
        }
    assert updates == []
    assert after == before


def test_explicit_historical_reevaluation_survives_idempotent_startup(
    tmp_path: Path, monkeypatch,
):
    for name in ("Main", "Fragment"):
        for episode in (1, 2):
            path = tmp_path / "Anime" / name / f"E{episode:02}.mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine(f"sqlite:///{tmp_path / 'historical-reevaluation.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, tmp_path)
        target = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Anime/Main"
        ))
        source = session.scalar(select(CatalogCollection).where(
            CatalogCollection.relative_root_path == "Anime/Fragment"
        ))
        manual = target.titles[0]
        moved = source.titles[0]
        manual.hierarchy_manual_override = True
        manual.part_type_manual = "season"
        manual.season_number_manual = 1
        manual.part_number_manual = None
        manual.season_label_manual = "S1"
        manual.hierarchy_verified_at = utc_now()
        move_titles_to_collection(session, target.id, [moved.id])
        record_manual_collection_merge(session, target, [moved.id])
        session.commit()
        target_id, manual_id, moved_id = target.id, manual.id, moved.id

        # Simulace historického stavu vytvořeného před collection-move fixem.
        moved.part_type = "season"
        moved.season_number = 1
        moved.part_number = None
        moved.season_label = "S1"
        session.commit()

        reevaluate_automatic_collection_hierarchy(session, target.id)
        session.commit()
        assert (
            moved.part_type, moved.season_number, moved.part_number,
            moved.season_label,
        ) == ("title", None, None, None)
        assert manual.effective_season_number == 1

    migrate_schema(engine)
    with Session(engine) as session:
        moved = session.get(CatalogTitle, moved_id)
        manual = session.get(CatalogTitle, manual_id)
        assert moved.catalog_collection_id == target_id
        assert (
            moved.part_type, moved.season_number, moved.part_number,
            moved.season_label,
        ) == ("title", None, None, None)
        assert (
            manual.hierarchy_manual_override,
            manual.part_type_manual,
            manual.season_number_manual,
            manual.part_number_manual,
        ) == (True, "season", 1, None)
        before = {
            title.id: (
                title.catalog_collection_id,
                title.part_type,
                title.season_number,
                title.part_number,
                title.season_label,
                title.updated_at,
            )
            for title in session.scalars(select(CatalogTitle))
        }

    updates = []

    def record_update(_connection, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE CATALOG_TITLES"):
            updates.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record_update)
    try:
        migrate_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", record_update)

    with Session(engine) as session:
        after = {
            title.id: (
                title.catalog_collection_id,
                title.part_type,
                title.season_number,
                title.part_number,
                title.season_label,
                title.updated_at,
            )
            for title in session.scalars(select(CatalogTitle))
        }
    assert updates == []
    assert after == before


def test_keep_collections_separate_survives_startup_rescan_and_restart(
    tmp_path: Path, monkeypatch,
):
    _high_school_dxd_grouping_files(tmp_path)
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine(f"sqlite:///{tmp_path / 'grouping-separate.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, tmp_path)
        suggestion = collection_grouping_suggestions(session)[0]
        original_paths = _populated_collection_paths(session)
        assert len(original_paths) == 2
        record_grouping_decision(session, suggestion, "separate")
        session.commit()

    migrate_schema(engine)
    with Session(engine) as session:
        assert _populated_collection_paths(session) == original_paths
        decision = session.scalar(select(CollectionGroupingDecision))
        assert decision.decision == "separate"
        assert decision.target_collection_path is None
        assert decision.selected_title_paths_json is None
        assert collection_grouping_suggestions(session) == []

        scan_library(session, tmp_path)

    migrate_schema(engine)
    with Session(engine) as session:
        assert _populated_collection_paths(session) == original_paths
        assert collection_grouping_suggestions(session) == []


def test_manual_move_form_recovers_legacy_suppressed_merge_and_persists_restart(
    tmp_path: Path, monkeypatch,
):
    _high_school_dxd_grouping_files(tmp_path)
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine(f"sqlite:///{tmp_path / 'grouping-recovery.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, tmp_path)
        suggestion = collection_grouping_suggestions(session)[0]
        target = session.get(CatalogCollection, suggestion.target_collection_id)
        moved_title = next(
            title for collection in suggestion.collections for title in collection.titles
            if title.collection is not target
        )
        session.add(CollectionGroupingDecision(
            suggestion_key=suggestion.key,
            state_fingerprint=suggestion.state_fingerprint,
            decision="merged",
        ))
        session.commit()
        assert collection_grouping_suggestions(session) == []

        move_titles_to_collection(session, target.id, [moved_title.id])
        record_manual_collection_merge(session, target, [moved_title.id])
        session.commit()
        target_path = target.relative_root_path
        moved_title_id = moved_title.id

    migrate_schema(engine)

    with Session(engine) as session:
        assert session.get(CatalogTitle, moved_title_id).collection.relative_root_path == (
            target_path
        )
        assert _populated_collection_paths(session) == {target_path}
        decisions = list(session.scalars(select(CollectionGroupingDecision)).all())
        assert len(decisions) == 2
        assert any(
            decision.target_collection_path == target_path
            and decision.selected_title_paths_json
            for decision in decisions
        )


def test_multiple_titles_can_move_and_operation_can_be_reversed():
    _, sessions = _sessions()
    with sessions() as session:
        source = CatalogCollection(
            local_title="Source", normalized_local_title="source", relative_root_path="Source",
        )
        target = CatalogCollection(
            local_title="Target", normalized_local_title="target", relative_root_path="Target",
        )
        titles = [
            CatalogTitle(
                collection=source, local_title=f"Part {number}",
                normalized_local_title=f"part {number}",
                relative_root_path=f"Source/Part {number}",
                season_number=number, part_type="season",
            )
            for number in (1, 2)
        ]
        for title in titles:
            Video(
                catalog_title=title, catalog_collection=source,
                relative_path=f"{title.relative_root_path}/E01.mkv", root_folder="Source",
                filename="E01.mkv", size=1, mtime_ns=1,
            )
        session.add_all([source, target])
        session.flush()

        move_titles_to_collection(session, target.id, [title.id for title in titles])
        session.commit()
        assert source.titles == []
        assert len(target.titles) == 2

        move_titles_to_collection(session, source.id, [titles[0].id])
        session.commit()
        assert [title.id for title in source.titles] == [titles[0].id]
        assert [title.id for title in target.titles] == [titles[1].id]


def test_empty_collection_is_only_deleted_by_explicit_backend_operation():
    _, sessions = _sessions()
    with sessions() as session:
        empty = CatalogCollection(
            local_title="Empty", normalized_local_title="empty", relative_root_path="Empty",
        )
        nonempty = CatalogCollection(
            local_title="Nonempty", normalized_local_title="nonempty",
            relative_root_path="Nonempty",
        )
        CatalogTitle(
            collection=nonempty, local_title="Part", normalized_local_title="part",
            relative_root_path="Nonempty/Part",
        )
        session.add_all([empty, nonempty])
        session.commit()
        empty_id, nonempty_id = empty.id, nonempty.id

        with pytest.raises(ValueError, match="bez částí a videí"):
            delete_empty_collection(session, nonempty_id)
        delete_empty_collection(session, empty_id)
        session.commit()

        assert session.get(CatalogCollection, empty_id) is None
        assert session.get(CatalogCollection, nonempty_id) is not None


def test_bulk_delete_removes_only_collections_still_empty_in_one_operation():
    engine, sessions = _sessions()
    with sessions() as session:
        empty_a = CatalogCollection(
            local_title="Empty A", normalized_local_title="empty a",
            relative_root_path="@manual/empty-a",
        )
        empty_b = CatalogCollection(
            local_title="Empty B", normalized_local_title="empty b",
            relative_root_path="@manual/empty-b",
        )
        changed = CatalogCollection(
            local_title="Changed", normalized_local_title="changed",
            relative_root_path="@manual/changed",
        )
        video_only = CatalogCollection(
            local_title="Video only", normalized_local_title="video only",
            relative_root_path="@manual/video-only",
        )
        CatalogTitle(
            collection=changed, local_title="New part", normalized_local_title="new part",
            relative_root_path="@manual/changed/new-part",
        )
        Video(
            catalog_collection=video_only,
            relative_path="Anime/Unassigned.mkv", root_folder="Anime",
            filename="Unassigned.mkv", size=1, mtime_ns=1,
        )
        session.add_all([empty_a, empty_b, changed, video_only])
        session.commit()
        ids = [empty_a.id, empty_b.id, changed.id, video_only.id]
        query_count = 0

        @event.listens_for(engine, "before_cursor_execute")
        def count_query(*_):
            nonlocal query_count
            query_count += 1

        result = delete_empty_collections(session, ids)
        session.commit()
        operation_query_count = query_count

        assert {name for _, name in result.deleted} == {"Empty A", "Empty B"}
        assert result.skipped == (
            (changed.id, "Changed"), (video_only.id, "Video only"),
        )
        assert session.get(CatalogCollection, empty_a.id) is None
        assert session.get(CatalogCollection, empty_b.id) is None
        assert session.get(CatalogCollection, changed.id) is not None
        assert session.get(CatalogCollection, video_only.id) is not None
        assert operation_query_count <= 6


def test_last_title_delete_keeps_collection_until_explicit_collection_delete():
    _, sessions = _sessions()
    with sessions() as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="@manual/show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Old OVA", normalized_local_title="old ova",
            relative_root_path="@manual/show/old-ova",
        )
        session.add(collection)
        session.commit()
        collection_id = collection.id

        delete_empty_local_title(session, collection_id, title.id)
        session.commit()

        assert session.get(CatalogCollection, collection_id) is not None
        assert session.get(CatalogCollection, collection_id).titles == []

        delete_empty_collection(session, collection_id)
        session.commit()
        assert session.get(CatalogCollection, collection_id) is None


def test_create_main_collection_uses_existing_titles_without_merging_them():
    _, sessions = _sessions()
    with sessions() as session:
        sources = []
        for number, name in enumerate(("Overlord I", "Overlord II"), 1):
            collection = CatalogCollection(
                local_title=name, normalized_local_title=normalize(name),
                relative_root_path=f"Anime/{name}",
            )
            title = CatalogTitle(
                collection=collection, local_title=name, normalized_local_title=normalize(name),
                relative_root_path=f"Anime/{name}", season_number=number,
            )
            Video(
                catalog_title=title, catalog_collection=collection,
                relative_path=f"Anime/{name}/E01.mkv", root_folder="Anime",
                filename="E01.mkv", size=number, mtime_ns=1,
            )
            sources.append((collection, title))
        session.add_all([item[0] for item in sources])
        session.flush()
        title_ids = [item[1].id for item in sources]

        target = create_main_collection(session, "Overlord", title_ids)
        session.commit()

        assert target.manual_display_title == "Overlord"
        assert {title.id for title in target.titles} == set(title_ids)
        assert len(target.titles) == 2
        assert {video.catalog_title_id for video in target.videos} == set(title_ids)


def normalize(value: str) -> str:
    return value.casefold()


def _suggestion_seed(session: Session):
    collections = []
    for name in ("High School DxD", "High School DxD Born", "Specials"):
        collection = CatalogCollection(
            local_title=name, normalized_local_title=normalize(name),
            relative_root_path=f"Anime/High School DxD franchise/{name}",
        )
        title = CatalogTitle(
            collection=collection, local_title=name, normalized_local_title=normalize(name),
            relative_root_path=collection.relative_root_path,
        )
        Video(
            catalog_title=title, catalog_collection=collection,
            relative_path=f"{collection.relative_root_path}/E01.mkv", root_folder="Anime",
            filename="E01.mkv", size=1, mtime_ns=1,
        )
        collections.append(collection)
    session.add_all(collections)
    session.flush()
    return collections


def test_grouping_suggestion_uses_parent_and_persists_keep_separate_decision():
    _, sessions = _sessions()
    with sessions() as session:
        collections = _suggestion_seed(session)
        suggestions = collection_grouping_suggestions(session)
        assert len(suggestions) == 1
        suggestion = suggestions[0]
        assert {item.id for item in suggestion.collections} == {
            collection.id for collection in collections
        }
        assert "společný fyzický parent" in " ".join(suggestion.reasons)

        record_grouping_decision(session, suggestion, "separate")
        session.commit()
        assert collection_grouping_suggestions(session) == []
        assert session.scalar(select(func.count()).select_from(
            CollectionGroupingDecision
        )) == 1

        collection = collections[0]
        session.add(CatalogTitle(
            collection=collection, local_title="NC", normalized_local_title="nc",
            relative_root_path=f"{collection.relative_root_path}/NC",
        ))
        session.commit()
        assert len(collection_grouping_suggestions(session)) == 1


def test_name_similarity_without_common_parent_produces_no_suggestion():
    _, sessions = _sessions()
    with sessions() as session:
        for root, name in (("A", "Peter Grill"), ("B", "Peter Grill Season 2")):
            collection = CatalogCollection(
                local_title=name, normalized_local_title=normalize(name),
                relative_root_path=f"{root}/{name}",
            )
            CatalogTitle(
                collection=collection, local_title=name, normalized_local_title=normalize(name),
                relative_root_path=collection.relative_root_path,
            )
            session.add(collection)
        session.flush()

        assert collection_grouping_suggestions(session) == []


def test_descendant_suggestion_prefers_existing_main_collection():
    _, sessions = _sessions()
    with sessions() as session:
        main = CatalogCollection(
            local_title="Uzaki-chan", normalized_local_title="uzaki chan",
            relative_root_path="Anime/Uzaki-chan",
        )
        extra = CatalogCollection(
            local_title="Season 2", normalized_local_title="season 2",
            relative_root_path="Anime/Uzaki-chan/Season 2",
        )
        CatalogTitle(
            collection=main, local_title="Season 1", normalized_local_title="season 1",
            relative_root_path="Anime/Uzaki-chan/Season 1",
        )
        CatalogTitle(
            collection=extra, local_title="Season 2", normalized_local_title="season 2",
            relative_root_path="Anime/Uzaki-chan/Season 2",
        )
        session.add_all([main, extra])
        session.flush()

        suggestion = collection_grouping_suggestions(session)[0]

        assert suggestion.target_collection_id == main.id
        assert suggestion.proposed_name == "Uzaki-chan"


def test_confirmed_physical_duplicate_survives_collection_move():
    _, sessions = _sessions()
    with sessions() as session:
        source = CatalogCollection(
            local_title="Uzaki-chan", normalized_local_title="uzaki chan",
            relative_root_path="Anime/Uzaki-chan Season 2",
        )
        target = CatalogCollection(
            local_title="Uzaki-chan", normalized_local_title="uzaki chan",
            relative_root_path="Anime/Uzaki-chan",
        )
        title = CatalogTitle(
            collection=source, local_title="Season 2", normalized_local_title="season 2",
            relative_root_path="Anime/Uzaki-chan Season 2", part_type="season",
            season_number=2,
        )
        videos = [
            Video(
                catalog_title=title, catalog_collection=source,
                relative_path=f"Anime/Uzaki-chan Season 2/{filename}", root_folder="Anime",
                filename=filename, size=size, mtime_ns=1,
                local_episode_number=episode, season_episode_number=episode,
                duplicate_status_manual=duplicate_status,
            )
            for filename, size, episode, duplicate_status in (
                ("E01.mkv", 1, 1, None),
                ("E01 copy.mkv", 2, 1, None),
                ("E02 maybe copy.mkv", 3, 2, "suspected"),
            )
        ]
        session.add_all([source, target])
        session.flush()
        metadata = TitleMetadata(
            catalog_title_id=title.id,
            display_title="Uzaki-chan Wants to Hang Out! Season 2",
            metadata_provider="anilist",
            metadata_external_id="124395",
        )
        link = ExternalTitleLink(
            catalog_title_id=title.id,
            provider="anilist",
            external_id="124395",
            match_method="manual_search",
            is_primary=True,
            is_manual=True,
        )
        session.add_all([metadata, link])
        session.flush()
        set_duplicate_group_primary(videos[:2], videos[0])
        duplicate_id, primary_id = videos[1].id, videos[0].id
        suspected_id = videos[2].id
        title_id = title.id
        membership = {
            video.id: (video.catalog_title_id, video.relative_path)
            for video in videos
        }

        move_titles_to_collection(session, target.id, [title.id])
        session.commit()

        assert session.get(Video, duplicate_id).duplicate_of_video_id == primary_id
        assert session.get(Video, suspected_id).duplicate_status_manual == "suspected"
        assert session.get(Video, suspected_id).duplicate_of_video_id is None
        assert session.get(CatalogTitle, title_id).metadata_record.metadata_external_id == (
            "124395"
        )
        assert session.get(CatalogTitle, title_id).external_links[0].external_id == (
            "124395"
        )
        assert len(title.videos) == 3
        assert len({video.relative_path for video in title.videos}) == 3
        assert {
            video.id: (video.catalog_title_id, video.relative_path)
            for video in title.videos
        } == membership
        assert all(video.catalog_collection_id == target.id for video in title.videos)


def test_hierarchy_review_renders_grouping_workflows_and_physical_evidence(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'grouping-ui.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collections = _suggestion_seed(session)
        collections[0].titles[0].videos[0].season_episode_number = 1
        session.commit()
        collection_id = collections[0].id
    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }
    with web_app.state.sessions() as session:
        engine = session.get_bind()
    query_count = 0

    @event.listens_for(engine, "before_cursor_execute")
    def count_query(*_):
        nonlocal query_count
        query_count += 1

    def request(path: str):
        return Request({
            "type": "http", "app": web_app, "method": "GET", "path": path,
            "root_path": "", "scheme": "http", "query_string": b"",
            "headers": [], "server": ("testserver", 80),
            "client": ("testclient", 50000),
        })

    overview = endpoints["/hierarchy-review"](
        request("/hierarchy-review")
    ).body.decode()
    overview_query_count = query_count
    detail = endpoints["/hierarchy-review/{collection_id}"](
        request(f"/hierarchy-review/{collection_id}"), collection_id
    ).body.decode()

    assert "Možné společné anime: High School DxD franchise" in overview
    assert "Vytvořit hlavní anime / collection" in overview
    assert "Sloučit collections / přesunout části" in overview
    assert "Ponechat jako samostatné anime" in overview
    assert "Anime/High School DxD franchise/High School DxD" in overview
    assert "Přesunout části do jiné collection" in detail
    assert 'name="title_ids"' in detail
    assert overview_query_count == 6


def test_grouping_candidate_discovery_is_bucketed_not_global_quadratic():
    _, sessions = _sessions()
    collection_count = 600
    videos_per_collection = 5
    with sessions() as session:
        collections = []
        for index in range(collection_count):
            group = index // 3
            part = index % 3 + 1
            collection = CatalogCollection(
                local_title=f"Show {group} Season {part}",
                normalized_local_title=f"show {group} season {part}",
                relative_root_path=f"Anime/Group {group}/Show {group} Season {part}",
            )
            title = CatalogTitle(
                collection=collection, local_title=f"Season {part}",
                normalized_local_title=f"season {part}",
                relative_root_path=collection.relative_root_path,
            )
            for episode in range(videos_per_collection):
                Video(
                    catalog_title=title, catalog_collection=collection,
                    relative_path=f"{collection.relative_root_path}/E{episode + 1:02}.mkv",
                    root_folder="Anime", filename=f"E{episode + 1:02}.mkv",
                    size=1, mtime_ns=1, season_episode_number=episode + 1,
                )
            collections.append(collection)
        session.add_all(collections)
        session.flush()
        metrics = CollectionGroupingMetrics()

        suggestions = collection_grouping_suggestions(
            session, collections=collections, metrics=metrics,
        )

        assert len(suggestions) == collection_count // 3
        assert metrics.ancestor_lookups == collection_count * 2
        assert metrics.sibling_name_comparisons == collection_count
        assert metrics.candidate_comparisons == 1800
        assert metrics.candidate_comparisons < collection_count * 4
        assert metrics.candidate_comparisons < collection_count ** 2 // 100
