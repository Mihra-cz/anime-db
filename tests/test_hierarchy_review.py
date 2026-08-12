from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.catalog import video_matches_filter
from app.hierarchy_review import (
    ManualTitleDefinition, apply_manual_split, apply_single_season_suggestion,
    classify_videos_in_place, collection_requires_review, create_title_from_videos,
    delete_empty_local_title, extract_local_period_hint, merge_title_into,
    move_videos_to_title, parse_manual_definitions, parse_simple_definitions,
    preview_assignments, separate_nonstandard_videos,
    simple_definition_rows,
    single_season_suggestion,
)
from app.models import (
    CatalogCollection, CatalogTitle, ExternalTitleLink, InternalSubtitle,
    TitleMetadata, Video, utc_now,
)
from app.migrations import migrate_schema
from app.numbering import (
    apply_sequential_numbering,
    collection_requires_numbering_review as numbering_requires_review,
    summarize_title_numbering,
)
from app.scanner import scan_library


PROBE_RESULT = {
    "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
    "audio": [], "subtitles": [],
}


def seeded_collection(count: int = 26):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Anime title (Z18-L20)",
            normalized_local_title="anime title z18 l20",
            relative_root_path="Anime/Anime title (Z18-L20)",
        )
        title = CatalogTitle(
            collection=collection, local_title=collection.local_title,
            normalized_local_title=collection.normalized_local_title,
            relative_root_path=collection.relative_root_path,
        )
        session.add_all([collection, title])
        session.flush()
        for number in range(1, count + 1):
            session.add(Video(
                relative_path=f"{collection.relative_root_path}/Episode {number:02}.mkv",
                root_folder="Anime", filename=f"Episode {number:02}.mkv",
                size=1, mtime_ns=1, local_episode_number=number,
                catalog_title=title, catalog_collection=collection,
            ))
        session.commit()
        return engine, collection.id, title.id


def definitions(first_title_id: int):
    return parse_manual_definitions(f"""[
      {{"title_id": {first_title_id}, "local_title": "Part 1", "season_number_manual": 1,
       "season_label_manual": "S1", "part_number": 1, "part_type_manual": "part",
       "episode_start": 1, "episode_end": 13, "episode_start_offset": 0,
       "numbering_mode": "absolute", "sort_order": 1}},
      {{"local_title": "Part 2", "season_number_manual": 2,
       "season_label_manual": "S2", "part_number": 2, "part_type_manual": "part",
       "episode_start": 14, "episode_end": 26, "episode_start_offset": 13,
       "numbering_mode": "absolute", "sort_order": 2}}
    ]""")


def simple_collection(*, part_type="title", status="review_required", with_video=True):
    collection = CatalogCollection(
        id=1, local_title="Akame ga Kill! (L14)",
        normalized_local_title="akame ga kill l14",
        relative_root_path="Anime/Akame ga Kill! (L14)", hierarchy_status=status,
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title=collection.local_title,
        normalized_local_title=collection.normalized_local_title,
        relative_root_path=collection.relative_root_path, part_type=part_type,
    )
    if with_video:
        Video(
            id=1, relative_path=f"{collection.relative_root_path}/Episode 01.mkv",
            root_folder="Anime", filename="Episode 01.mkv", size=1, mtime_ns=1,
            local_episode_number=1, season_episode_number=1,
            absolute_episode_number=1, catalog_title=title,
            catalog_collection=collection,
        )
    return collection, title


def test_flat_collection_with_episodes_1_to_26_requires_review():
    engine, collection_id, _ = seeded_collection()
    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        reason = collection_requires_review(collection, list(collection.videos))
        assert reason is not None


def test_internal_period_hint_does_not_infer_season_count_or_change_identity():
    engine, collection_id, title_id = seeded_collection()
    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        title = session.get(CatalogTitle, title_id)
        assert extract_local_period_hint(collection.local_title) == "Z18-L20"
        assert title.season_number is None
        assert collection.local_title == "Anime title (Z18-L20)"
        assert collection.relative_root_path == "Anime/Anime title (Z18-L20)"


def test_single_generic_title_gets_manual_season_one_suggestion():
    collection, title = simple_collection()
    title.metadata_record = TitleMetadata(
        catalog_title_id=title.id, display_title="Akame ga Kill!",
        format="TV", episode_count=24,
    )

    suggestion = single_season_suggestion(collection)

    assert suggestion is not None
    assert suggestion.title is title
    assert suggestion.metadata_supports_tv is True


def test_confirmed_season_one_suggestion_changes_only_manual_title_hierarchy():
    collection, title = simple_collection()
    collection_status = collection.hierarchy_status
    video = title.videos[0]
    original_title = video.catalog_title
    episode_values = (
        video.local_episode_number, video.season_episode_number,
        video.absolute_episode_number, video.external_episode_number,
    )
    metadata = TitleMetadata(
        catalog_title_id=title.id, display_title="Akame ga Kill!",
        metadata_provider="anilist", metadata_external_id="20613", format="TV",
    )
    link = ExternalTitleLink(
        catalog_title_id=title.id, provider="anilist", external_id="20613",
        match_method="manual_search", is_primary=True, is_manual=True,
    )
    title.metadata_record = metadata
    title.external_links.append(link)

    changed = apply_single_season_suggestion(collection)

    assert changed is title
    assert title.season_number_manual == 1
    assert title.season_label_manual == "S1"
    assert title.part_type_manual == "season"
    assert title.hierarchy_manual_override is True
    assert title.hierarchy_verified_at is None
    assert collection.hierarchy_status == collection_status
    assert collection.hierarchy_verified_at is None
    assert video.catalog_title is original_title
    assert (
        video.local_episode_number, video.season_episode_number,
        video.absolute_episode_number, video.external_episode_number,
    ) == episode_values
    assert title.metadata_record is metadata
    assert title.external_links == [link]


@pytest.mark.parametrize("part_type", ["film", "ova"])
def test_non_series_manual_type_does_not_get_season_one_suggestion(part_type):
    collection, title = simple_collection()
    title.part_type_manual = part_type
    title.hierarchy_manual_override = True

    assert single_season_suggestion(collection) is None


def test_collection_with_two_titles_does_not_get_season_one_suggestion():
    collection, _ = simple_collection()
    CatalogTitle(
        id=2, collection=collection, local_title="Second title",
        normalized_local_title="second title",
        relative_root_path=f"{collection.relative_root_path}/Second title",
        part_type="title",
    )

    assert single_season_suggestion(collection) is None


def test_manual_season_two_does_not_get_season_one_suggestion():
    collection, title = simple_collection()
    title.season_number_manual = 2
    title.season_label_manual = "S2"
    title.part_type_manual = "season"
    title.hierarchy_manual_override = True

    assert single_season_suggestion(collection) is None


def test_conflicting_hierarchy_does_not_get_season_one_suggestion():
    collection, _ = simple_collection(status="conflict")

    assert single_season_suggestion(collection) is None


def test_title_without_videos_does_not_get_season_one_suggestion():
    collection, _ = simple_collection(with_video=False)

    assert single_season_suggestion(collection) is None


def test_existing_automatic_season_one_does_not_get_duplicate_suggestion():
    collection, title = simple_collection()
    title.season_number = 1
    title.season_label = "S1"
    title.part_type = "season"

    assert single_season_suggestion(collection) is None


def test_previously_verified_title_structure_does_not_get_suggestion():
    collection, title = simple_collection()
    title.hierarchy_verified_at = utc_now()

    assert single_season_suggestion(collection) is None


def test_suggestion_and_simple_preview_data_are_read_only():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, _ = simple_collection()
        session.add(collection)
        session.commit()

        assert single_season_suggestion(collection) is not None
        rows = simple_definition_rows(collection)
        definitions = parse_simple_definitions(rows)
        preview_assignments(list(collection.videos), definitions)

        assert not session.dirty
        assert not session.new
        assert not session.deleted


def test_simple_definition_form_uses_existing_backend_semantics():
    rows = [
        {
            "title_id": "1", "local_title": "Part 1",
            "season_number_manual": "1", "season_label_manual": "S1",
            "part_number": "1", "part_type_manual": "part",
            "episode_start": "1", "episode_end": "12",
            "episode_start_offset": "0", "numbering_mode": "absolute",
            "sort_order": "1", "filename_pattern": "", "video_ids": "1, 2 3",
        },
        {
            "title_id": "", "local_title": "", "numbering_mode": "unknown",
        },
    ]

    parsed = parse_simple_definitions(rows)

    assert len(parsed) == 1
    assert parsed[0] == ManualTitleDefinition(
        title_id=1, local_title="Part 1", manual_display_title=None,
        season_number_manual=1, season_label_manual="S1", part_number=1,
        part_type_manual="part", episode_start=1, episode_end=12,
        episode_start_offset=0, numbering_mode="absolute", sort_order=1,
        filename_pattern=None, video_ids=(1, 2, 3),
    )


def test_manual_split_creates_two_titles_and_assigns_ranges():
    engine, collection_id, title_id = seeded_collection()
    with Session(engine) as session:
        preview = apply_manual_split(session, collection_id, definitions(title_id))
        session.commit()
        assert not preview.conflicts
        assert not preview.unmatched_video_ids
        titles = session.scalars(select(CatalogTitle).order_by(CatalogTitle.sort_order_manual)).all()
        assert [title.local_title for title in titles] == ["Part 1", "Part 2"]
        assert [len(title.videos) for title in titles] == [13, 13]
        assert titles[1].videos[0].season_episode_number == 1
        collection = session.get(CatalogCollection, collection_id)
        assert collection.hierarchy_status == "verified"
        assert collection.hierarchy_verified_at is not None


def test_overlapping_ranges_require_explicit_confirmation():
    engine, collection_id, title_id = seeded_collection()
    overlap = definitions(title_id)
    overlap[1] = ManualTitleDefinition(
        **{**overlap[1].__dict__, "episode_start": 13}
    )
    with Session(engine) as session:
        preview = preview_assignments(list(session.get(CatalogCollection, collection_id).videos), overlap)
        assert preview.conflicts
        with pytest.raises(ValueError, match="explicitní potvrzení"):
            apply_manual_split(session, collection_id, overlap)


def test_video_outside_ranges_stays_unassigned():
    engine, collection_id, title_id = seeded_collection(27)
    with Session(engine) as session:
        preview = apply_manual_split(session, collection_id, definitions(title_id))
        session.commit()
        last = session.scalar(select(Video).where(Video.filename == "Episode 27.mkv"))
        assert last.id in preview.unmatched_video_ids
        assert last.catalog_title_id is None
        assert last.catalog_collection_id == collection_id
        assert session.get(CatalogCollection, collection_id).hierarchy_status == "review_required"


def test_individual_video_selection_and_filename_rule_are_previewed():
    engine, collection_id, title_id = seeded_collection(2)
    with Session(engine) as session:
        videos = list(session.get(CatalogCollection, collection_id).videos)
        selected = ManualTitleDefinition(
            title_id=title_id, local_title="Vybrané", manual_display_title=None,
            season_number_manual=None, season_label_manual=None, part_number=None,
            part_type_manual="special", episode_start=None, episode_end=None,
            episode_start_offset=None, numbering_mode="unknown", sort_order=1,
            video_ids=(videos[0].id,),
        )
        pattern = ManualTitleDefinition(
            title_id=None, local_title="Podle názvu", manual_display_title=None,
            season_number_manual=None, season_label_manual=None, part_number=None,
            part_type_manual="special", episode_start=None, episode_end=None,
            episode_start_offset=None, numbering_mode="unknown", sort_order=2,
            filename_pattern=r"Episode 02\.mkv$",
        )
        preview = preview_assignments(videos, [selected, pattern])
        assert preview.assignments == {videos[0].id: 0, videos[1].id: 1}


def test_external_link_is_only_a_hint_and_does_not_split_collection():
    engine, collection_id, title_id = seeded_collection()
    with Session(engine) as session:
        session.add(ExternalTitleLink(
            catalog_title_id=title_id, provider="anilist", external_id="123",
            match_method="manual_search", is_primary=True, is_manual=True,
        ))
        session.commit()
        preview_assignments(
            list(session.get(CatalogCollection, collection_id).videos), definitions(title_id)
        )
        assert session.scalar(select(func.count()).select_from(CatalogTitle)) == 1


def test_hierarchy_filters_include_unassigned_review_and_conflict():
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show", relative_root_path="Show",
        hierarchy_status="review_required",
    )
    video = Video(
        relative_path="Show/E01.mkv", root_folder="Show", filename="E01.mkv",
        size=1, mtime_ns=1, catalog_collection=collection,
    )
    assert video_matches_filter(video, "unassigned") is True
    assert video_matches_filter(video, "hierarchy-review") is True
    collection.hierarchy_status = "conflict"
    assert video_matches_filter(video, "hierarchy-conflict") is True


def test_verified_hierarchy_with_unknown_episode_stays_in_review_filter():
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show", relative_root_path="Show",
        hierarchy_status="verified",
    )
    title = CatalogTitle(
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Show/Season 1", collection=collection, season_number=1,
    )
    video = Video(
        relative_path="Show/Season 1/unknown.mkv", root_folder="Show",
        filename="unknown.mkv", size=1, mtime_ns=1,
        catalog_collection=collection, catalog_title=title,
    )

    assert video_matches_filter(video, "hierarchy-review") is True
    video.season_episode_number = 1
    assert video_matches_filter(video, "hierarchy-review") is False


def test_manual_split_preserves_metadata_subtitles_and_hardsub():
    engine, collection_id, title_id = seeded_collection()
    with Session(engine) as session:
        video = session.scalar(select(Video).where(Video.filename == "Episode 01.mkv"))
        video.manual_hardsub_cs = True
        session.add(InternalSubtitle(
            video_id=video.id, stream_index=1, language="cze", normalized_language="cs",
        ))
        session.add(ExternalTitleLink(
            catalog_title_id=title_id, provider="anilist", external_id="1",
            match_method="manual_search", is_primary=True, is_manual=True,
        ))
        apply_manual_split(session, collection_id, definitions(title_id))
        session.commit()
        video = session.get(Video, video.id)
        assert video.manual_hardsub_cs is True
        assert video.internal_subtitles[0].normalized_language == "cs"
        assert session.scalar(select(ExternalTitleLink).where(
            ExternalTitleLink.catalog_title_id == title_id
        )).external_id == "1"

    migrate_schema(engine)
    with Session(engine) as session:
        video = session.scalar(select(Video).where(Video.filename == "Episode 01.mkv"))
        assert video.manual_hardsub_cs is True
        assert video.internal_subtitles[0].normalized_language == "cs"
        assert video.catalog_title_id == title_id
        assert session.scalar(select(ExternalTitleLink).where(
            ExternalTitleLink.catalog_title_id == title_id
        )).external_id == "1"


def test_verified_manual_split_survives_scan_and_new_video_reopens_review(
    tmp_path: Path, monkeypatch,
):
    root = tmp_path
    folder = root / "Anime" / "Anime title (Z18-L20)"
    folder.mkdir(parents=True)
    for number in range(1, 27):
        (folder / f"Episode {number:02}.mkv").write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        scan_library(session, root)
        collection = session.scalar(select(CatalogCollection))
        original_title = collection.titles[0]
        assert collection.hierarchy_status == "review_required"
        apply_manual_split(session, collection.id, definitions(original_title.id))
        session.commit()
        split_ids = {title.id for title in collection.titles}

        scan_library(session, root)
        collection = session.get(CatalogCollection, collection.id)
        assert collection.hierarchy_status == "verified"
        assert {video.catalog_title_id for video in collection.videos} == split_ids

        (folder / "Episode 27.mkv").write_bytes(b"video")
        scan_library(session, root)
        collection = session.get(CatalogCollection, collection.id)
        new_video = session.scalar(select(Video).where(Video.filename == "Episode 27.mkv"))
        assert collection.hierarchy_status == "review_required"
        assert collection.hierarchy_note == "Nové nezařazené video."
        assert new_video.catalog_title_id is None


def test_separating_zero_persists_without_metadata_or_physical_path_change(
    tmp_path: Path, monkeypatch,
):
    folder = tmp_path / "Ansatsu Kyoushitsu (Z15-Z16)" / "Serie 1"
    folder.mkdir(parents=True)
    paths = [folder / f"Ansatsu Kyoushitsu {number:02}.mp4" for number in range(23)]
    for path in paths:
        path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        collection = session.scalar(select(CatalogCollection))
        zero = session.scalar(select(Video).where(Video.filename == "Ansatsu Kyoushitsu 00.mp4"))
        season = zero.catalog_title
        original_relative_path = zero.relative_path
        assert collection.hierarchy_status == "review_required"
        assert zero.local_episode_number is None
        assert zero.episode_number_source == "nonstandard_zero"

        preview = separate_nonstandard_videos(
            session, collection.id, [zero.id], local_title="Preview", part_type="preview",
        )
        session.commit()
        preview_id = preview.id
        season_id = season.id

    with sessions() as session:
        zero = session.scalar(select(Video).where(Video.filename == "Ansatsu Kyoushitsu 00.mp4"))
        preview = session.get(CatalogTitle, preview_id)
        season = session.get(CatalogTitle, season_id)
        assert zero.catalog_title_id == preview_id
        assert zero.relative_path == original_relative_path
        assert preview.effective_part_type == "preview"
        assert preview.metadata_record is None
        assert preview.external_links == []
        assert [video.season_episode_number for video in sorted(
            season.videos, key=lambda item: item.season_episode_number
        )] == list(range(1, 23))
        season_summary = summarize_title_numbering(list(season.videos), season)
        assert season_summary.numbered == season_summary.standard_total == 22
        assert season_summary.nonstandard == 0
        assert season_summary.requires_review is False
        assert numbering_requires_review(preview.collection) is False

        scan_library(session, tmp_path)

    with sessions() as session:
        zero = session.scalar(select(Video).where(Video.filename == "Ansatsu Kyoushitsu 00.mp4"))
        collection = session.scalar(select(CatalogCollection))
        assert zero.catalog_title_id == preview_id
        assert zero.relative_path == original_relative_path
        assert collection.hierarchy_status == "verified"
        assert [path.read_bytes() for path in paths] == [b"video"] * 23


def test_fractional_recap_can_stay_in_season_and_survives_session_and_scan(
    tmp_path: Path, monkeypatch,
):
    folder = tmp_path / "Arifureta" / "Season 1"
    folder.mkdir(parents=True)
    for filename in [
        *[f"Arifureta - {number:02}.mkv" for number in range(1, 13)],
        "Arifureta - 05.5.mkv",
    ]:
        (folder / filename).write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        collection = session.scalar(select(CatalogCollection))
        recap = session.scalar(select(Video).where(Video.filename == "Arifureta - 05.5.mkv"))
        season_id = recap.catalog_title_id
        original_path = recap.relative_path
        classify_videos_in_place(session, collection.id, [recap.id], "recap")
        session.commit()
        recap_id = recap.id
        collection_id = collection.id

    with sessions() as session:
        recap = session.get(Video, recap_id)
        collection = session.get(CatalogCollection, collection_id)
        summary = summarize_title_numbering(list(recap.catalog_title.videos), recap.catalog_title)
        assert recap.catalog_title_id == season_id
        assert recap.content_type_manual == "recap"
        assert recap.relative_path == original_path
        assert summary.standard_total == 12
        assert summary.resolved_supplemental == 1
        assert summary.requires_review is False
        assert collection.hierarchy_status == "verified"

        scan_library(session, tmp_path)
        recap = session.get(Video, recap_id)
        assert recap.catalog_title_id == season_id
        assert recap.content_type_manual == "recap"
        assert recap.relative_path == original_path


def test_move_merge_and_explicit_delete_change_only_logical_assignment():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Arifureta", normalized_local_title="arifureta",
            relative_root_path="Anime/Arifureta",
        )
        season = CatalogTitle(
            collection=collection, local_title="Season 1", normalized_local_title="season 1",
            relative_root_path="Anime/Arifureta/Season 1", part_type_manual="season",
        )
        recap_part = CatalogTitle(
            collection=collection, local_title="Recap", normalized_local_title="recap",
            relative_root_path="Anime/Arifureta/.catalog-part-2", part_type_manual="recap",
            hierarchy_manual_override=True,
        )
        recap = Video(
            relative_path="Anime/Arifureta/Season 1/Arifureta 05.5.mkv",
            root_folder="Anime", filename="Arifureta 05.5.mkv", size=1, mtime_ns=1,
            catalog_title=recap_part, catalog_collection=collection,
        )
        regular = Video(
            relative_path="Anime/Arifureta/Season 1/Arifureta 01.mkv",
            root_folder="Anime", filename="Arifureta 01.mkv", size=1, mtime_ns=1,
            catalog_title=recap_part, catalog_collection=collection,
        )
        session.add(collection)
        session.commit()
        collection_id, season_id, source_id = collection.id, season.id, recap_part.id
        recap_id, regular_id = recap.id, regular.id
        paths = {recap.id: recap.relative_path, regular.id: regular.relative_path}

        move_videos_to_title(session, collection_id, [recap_id], season_id)
        assert recap.catalog_title_id == season_id
        assert recap.content_type_manual == "recap"
        assert recap.relative_path == paths[recap_id]

        merge_title_into(session, collection_id, source_id, season_id)
        session.commit()
        assert session.get(Video, regular_id).catalog_title_id == season_id
        assert session.get(Video, regular_id).relative_path == paths[regular_id]
        assert session.get(CatalogTitle, source_id) is not None
        assert session.get(CatalogTitle, source_id).videos == []

        delete_empty_local_title(session, collection_id, source_id)
        session.commit()
        assert session.get(CatalogTitle, source_id) is None


def test_move_to_existing_title_survives_subsequent_scan(tmp_path: Path, monkeypatch):
    first_folder = tmp_path / "Arifureta" / "Season 1"
    second_folder = tmp_path / "Arifureta" / "Season 2"
    first_folder.mkdir(parents=True)
    second_folder.mkdir(parents=True)
    moved_path = first_folder / "Arifureta 05.5.mkv"
    moved_path.write_bytes(b"video")
    (second_folder / "Arifureta S2 01.mkv").write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        collection = session.scalar(select(CatalogCollection))
        moved = session.scalar(select(Video).where(Video.filename == moved_path.name))
        target = session.scalar(select(CatalogTitle).where(CatalogTitle.local_title == "Season 2"))
        original_path = moved.relative_path
        move_videos_to_title(session, collection.id, [moved.id], target.id)
        session.commit()
        moved_id, target_id = moved.id, target.id

        scan_library(session, tmp_path)
        moved = session.get(Video, moved_id)
        assert moved.catalog_title_id == target_id
        assert moved.relative_path == original_path
        assert moved_path.read_bytes() == b"video"


def test_standard_and_ova_part_videos_can_be_split_into_two_local_ova_titles():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Arifureta", normalized_local_title="arifureta",
            relative_root_path="Anime/Arifureta",
        )
        source = CatalogTitle(
            collection=collection, local_title="OVA", normalized_local_title="ova",
            relative_root_path="Anime/Arifureta/OVA",
        )
        names = [
            "Arifureta OVA Episode 01 Yue's Diary.mkv",
            "Arifureta OVA Episode 02 Hot Love Springs Eternal.mkv",
            "Arifureta S2 - OVA P1.mkv", "Arifureta S2 - OVA P2.mkv",
        ]
        items = [Video(
            relative_path=f"Anime/Arifureta/OVA/{name}", root_folder="Anime",
            filename=name, size=1, mtime_ns=1, catalog_title=source,
            catalog_collection=collection,
        ) for name in names]
        session.add(collection)
        session.flush()
        from app.numbering import recalculate_title_numbering
        recalculate_title_numbering(source, items)
        first = create_title_from_videos(
            session, collection.id, [items[0].id, items[1].id],
            local_title="OVA – Serie 1", part_type="ova",
        )
        second = create_title_from_videos(
            session, collection.id, [items[2].id, items[3].id],
            local_title="OVA – Serie 2", part_type="ova",
        )
        session.commit()

        assert [video.season_episode_number for video in first.videos] == [1, 2]
        assert [video.season_episode_number for video in second.videos] == [1, 2]
        assert first.effective_part_type == second.effective_part_type == "ova"
        assert first.metadata_record is second.metadata_record is None


def test_selected_bulk_numbering_is_deterministic_and_preserves_paths():
    items = [Video(
        id=index, relative_path=f"Anime/OVA/OVA P{name}.mkv", root_folder="Anime",
        filename=f"OVA P{name}.mkv", size=1, mtime_ns=1,
    ) for index, name in [(2, "2"), (1, "1")]]
    paths = [video.relative_path for video in items]

    rows = apply_sequential_numbering(items, 1)

    assert [(row.filename, row.proposed_episode) for row in rows] == [
        ("OVA P1.mkv", 1), ("OVA P2.mkv", 2),
    ]
    assert [video.relative_path for video in items] == paths
