import asyncio
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.catalog import (
    detect_episode_number,
    effective_video_content_type,
    effective_video_content_display,
    sort_title_videos,
)
from app.catalog_video_presentation import build_catalog_title_video_presentation
from app.config import Settings
from app.database import Base
from app.hierarchy_authority import activate_manual_hierarchy_snapshot
from app.hierarchy_evaluation import finalize_collection_hierarchy
from app.hierarchy_review import classify_videos_in_place
from app.main import create_app
from app.migrations import migrate_schema
from app.models import (
    CatalogCollection,
    CatalogTitle,
    ExternalTitleLink,
    TitleMetadata,
    Video,
    VideoVariantGroup,
    utc_now,
)
from app.numbering import (
    BulkRenumberMetrics,
    apply_deterministic_bulk_renumber,
    deterministic_bulk_renumber_proposal,
    effective_recap_episode_number,
    effective_video_numbering,
    logical_episode_partitions,
    manual_episode_number_input_value,
    manual_recap_episode_number,
    recalculate_title_numbering,
    set_video_episode_number_from_input,
    set_video_episode_override,
    summarize_title_numbering,
    validate_recap_number_for_content_type,
)
from app.scanner import scan_library
from app.supplementary import supplementary_review_issues


PROBE_RESULT = {
    "duration": 60.0,
    "video_codec": "h264",
    "width": 1920,
    "height": 1080,
    "audio": [],
    "subtitles": [],
}


def _graph(
    standard_numbers,
    *,
    recap_positions=("14.5",),
    expected_count=24,
    explicit_ids=True,
):
    collection = CatalogCollection(
        id=1 if explicit_ids else None,
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        id=10 if explicit_ids else None,
        collection=collection,
        local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1",
        part_type="season",
        season_number=1,
        season_label="S1",
        metadata_status="linked_manual" if expected_count is not None else "unlinked",
    )
    activate_manual_hierarchy_snapshot(
        title,
        part_type="season",
        season_number=1,
        part_number=None,
        season_label="S1",
        sort_order=0,
        verified_at=utc_now(),
    )
    if expected_count is not None:
        title.metadata_record = TitleMetadata(
            catalog_title_id=title.id,
            display_title="Show",
            episode_count=expected_count,
        )
        title.external_links.append(ExternalTitleLink(
            provider="anilist",
            external_id="123",
            match_method="manual_search",
            is_primary=True,
            is_manual=True,
            verified_at=utc_now(),
        ))

    videos = []
    identifier = 1
    for number in standard_numbers:
        videos.append(Video(
            id=identifier if explicit_ids else None,
            relative_path=f"{title.relative_root_path}/Show - {number:02d}.mkv",
            root_folder="Anime",
            filename=f"Show - {number:02d}.mkv",
            size=identifier,
            mtime_ns=identifier,
            file_type="episode",
            catalog_collection=collection,
            catalog_title=title,
        ))
        identifier += 1
    recaps = []
    for index, position in enumerate(recap_positions, 1):
        recap = Video(
            id=identifier if explicit_ids else None,
            relative_path=f"{title.relative_root_path}/Inserted Recap {index}.mkv",
            root_folder="Anime",
            filename=f"Inserted Recap {index}.mkv",
            size=identifier,
            mtime_ns=identifier,
            file_type="episode",
            content_type_manual="recap",
            catalog_collection=collection,
            catalog_title=title,
        )
        set_video_episode_number_from_input(recap, position)
        recaps.append(recap)
        videos.append(recap)
        identifier += 1
    recalculate_title_numbering(title, videos)
    return collection, title, videos, recaps


def _standard_by_number(title, number):
    return next(
        video for video in title.videos
        if video.season_episode_number == number
        and video.content_type_manual is None
        and video.duplicate_of_video_id is None
    )


def _request(web_app, path):
    return Request({
        "type": "http",
        "app": web_app,
        "method": "GET",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })


def _post_form_request(web_app, path, items):
    body = urlencode(items).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "app": web_app,
        "method": "POST",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }, receive)


def test_manual_input_is_integer_for_standard_and_one_decimal_for_recap():
    _collection, title, _videos, recaps = _graph(range(1, 3), expected_count=None)
    standard = _standard_by_number(title, 1)

    set_video_episode_number_from_input(standard, "14")
    assert standard.episode_number_manual_override == 14
    with pytest.raises(ValueError, match="celé číslo"):
        set_video_episode_number_from_input(standard, "14.5")

    for raw in ("14.5", "24.5", "24.9"):
        set_video_episode_number_from_input(recaps[0], raw)
        assert manual_episode_number_input_value(recaps[0]) == raw
        assert manual_recap_episode_number(recaps[0]) == Decimal(raw)

    set_video_episode_number_from_input(recaps[0], "14")
    assert manual_episode_number_input_value(recaps[0]) == "14"
    assert recaps[0].recap_episode_number_manual_tenths == 140


@pytest.mark.parametrize("raw", ["14.55", "14.", ".5", "0.5", "-1.5", "text"])
def test_recap_server_validation_rejects_invalid_bypassed_html_values(raw):
    _collection, _title, _videos, recaps = _graph(range(1, 3), expected_count=None)
    with pytest.raises(ValueError, match="Recap číslo"):
        set_video_episode_number_from_input(recaps[0], raw)


def test_fractional_recap_round_trips_database_exactly_and_type_change_is_safe():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _collection, _title, videos, recaps = _graph(
            range(1, 3), recap_positions=("24.9",), expected_count=None,
            explicit_ids=False,
        )
        session.add_all(videos)
        session.commit()
        recap_id = recaps[0].id

    with Session(engine) as session:
        stored = session.get(Video, recap_id)
        assert stored.recap_episode_number_manual_tenths == 249
        assert manual_recap_episode_number(stored) == Decimal("24.9")
        assert manual_episode_number_input_value(stored) == "24.9"
        with pytest.raises(ValueError, match="nebude smazána automaticky"):
            validate_recap_number_for_content_type(stored, "episode")
        assert stored.recap_episode_number_manual_tenths == 249


def test_fractional_recap_reclassification_requires_explicit_number_clear():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, title, videos, recaps = _graph(
            range(1, 3), expected_count=None, explicit_ids=False,
        )
        session.add(collection)
        session.flush()
        recalculate_title_numbering(title, videos)
        recap = recaps[0]

        with pytest.raises(ValueError, match="nebude smazána automaticky"):
            classify_videos_in_place(session, collection.id, [recap.id], "bonus")
        assert recap.content_type_manual == "recap"
        assert recap.recap_episode_number_manual_tenths == 145

        set_video_episode_number_from_input(recap, "")
        classify_videos_in_place(session, collection.id, [recap.id], "")
        assert recap.content_type_manual is None
        assert recap.recap_episode_number_manual_tenths is None


def test_explicit_recap_to_bonus_keeps_raw_evidence_and_uses_integer_ordinal():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, title, _videos, _recaps = _graph(
            range(1, 3), recap_positions=(), expected_count=None,
            explicit_ids=False,
        )
        recap = Video(
            relative_path="Anime/Show/Season 1/Recap 3.5.mkv",
            root_folder="Anime",
            filename="Recap 3.5.mkv",
            size=10,
            mtime_ns=10,
            file_type="recap",
            content_type_manual="recap",
            catalog_collection=collection,
            catalog_title=title,
        )
        session.add(collection)
        session.flush()
        set_video_episode_number_from_input(recap, "5.5")

        with pytest.raises(ValueError, match="nebude smazána automaticky"):
            classify_videos_in_place(session, collection.id, [recap.id], "bonus")
        assert recap.content_type_manual == "recap"
        assert effective_recap_episode_number(recap) == Decimal("5.5")

        set_video_episode_number_from_input(recap, "")
        classify_videos_in_place(session, collection.id, [recap.id], "bonus")

        assert recap.file_type == "recap"
        assert effective_video_content_type(recap) == "bonus"
        assert effective_recap_episode_number(recap) is None
        assert effective_video_numbering(recap).supplementary_number is None
        assert supplementary_review_issues([recap], title) == ()

        set_video_episode_number_from_input(recap, "5")
        state = effective_video_numbering(recap)
        assert (state.supplementary_type, state.supplementary_number) == (
            "bonus", 5,
        )
        with pytest.raises(ValueError, match="celé číslo"):
            set_video_episode_number_from_input(recap, "5.5")

        second = Video(
            relative_path="Anime/Show/Season 1/Bonus interview.mkv",
            root_folder="Anime",
            filename="Bonus interview.mkv",
            size=11,
            mtime_ns=11,
            file_type="other",
            content_type_manual="bonus",
            catalog_collection=collection,
            catalog_title=title,
        )
        assert [issue.code for issue in supplementary_review_issues(
            [recap, second], title,
        )] == ["missing_supplementary_ordinal"]


def test_public_move_rejects_effective_recap_outside_season_and_allows_season(
    tmp_path: Path,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'recap-move.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        season_one = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1",
        )
        season_two = CatalogTitle(
            collection=collection,
            local_title="Season 2",
            normalized_local_title="season 2",
            relative_root_path="Anime/Show/Season 2",
        )
        bonus = CatalogTitle(
            collection=collection,
            local_title="Bonus",
            normalized_local_title="bonus",
            relative_root_path="Anime/Show/.catalog-part-bonus",
        )
        for title, part_type, season_number, label in (
            (season_one, "season", 1, "S1"),
            (season_two, "season", 2, "S2"),
            (bonus, "bonus", None, "Bonus"),
        ):
            activate_manual_hierarchy_snapshot(
                title,
                part_type=part_type,
                season_number=season_number,
                part_number=None,
                season_label=label,
                sort_order=None,
                verified_at=utc_now(),
            )
        manual = Video(
            relative_path="Anime/Show/Season 1/Manual recap.mkv",
            root_folder="Anime", filename="Manual recap.mkv", size=1, mtime_ns=1,
            file_type="episode", content_type_manual="recap",
            catalog_collection=collection, catalog_title=season_one,
        )
        parser = Video(
            relative_path="Anime/Show/Season 1/Recap 3.5.mkv",
            root_folder="Anime", filename="Recap 3.5.mkv", size=2, mtime_ns=2,
            file_type="recap", catalog_collection=collection,
            catalog_title=season_one,
        )
        raw_manual = Video(
            relative_path="Anime/Show/Season 1/Raw recap.mkv",
            root_folder="Anime", filename="Raw recap.mkv", size=3, mtime_ns=3,
            file_type="recap", catalog_collection=collection,
            catalog_title=season_one,
        )
        valid = Video(
            relative_path="Anime/Show/Season 1/Valid recap.mkv",
            root_folder="Anime", filename="Valid recap.mkv", size=4, mtime_ns=4,
            file_type="episode", content_type_manual="recap",
            catalog_collection=collection, catalog_title=season_one,
        )
        session.add(collection)
        session.flush()
        set_video_episode_number_from_input(manual, "24.9")
        set_video_episode_number_from_input(raw_manual, "24.9")
        set_video_episode_number_from_input(valid, "5.5")
        session.commit()
        ids = {
            "collection": collection.id,
            "season_one": season_one.id,
            "season_two": season_two.id,
            "bonus": bonus.id,
            "manual": manual.id,
            "parser": parser.id,
            "raw_manual": raw_manual.id,
            "valid": valid.id,
        }

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    endpoint = endpoints["/hierarchy-review/{collection_id}/manage-videos"]
    path = f"/hierarchy-review/{ids['collection']}/manage-videos"
    for name in ("manual", "parser", "raw_manual"):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(endpoint(_post_form_request(web_app, path, [
                ("video_ids", str(ids[name])),
                ("operation", "move"),
                ("target_title_id", str(ids["bonus"])),
            ]), ids["collection"]))
        assert exc_info.value.status_code == 400
        assert "Season kontextu" in exc_info.value.detail

    response = asyncio.run(endpoint(_post_form_request(web_app, path, [
        ("video_ids", str(ids["valid"])),
        ("operation", "move"),
        ("target_title_id", str(ids["season_two"])),
    ]), ids["collection"]))
    assert response.status_code == 303

    with web_app.state.sessions() as session:
        for name in ("manual", "parser", "raw_manual"):
            stored = session.get(Video, ids[name])
            assert stored.catalog_title_id == ids["season_one"]
        assert session.get(Video, ids["manual"]).recap_episode_number_manual_tenths == 249
        assert session.get(Video, ids["raw_manual"]).recap_episode_number_manual_tenths == 249
        moved = session.get(Video, ids["valid"])
        assert moved.catalog_title_id == ids["season_two"]
        assert moved.content_type_manual == "recap"
        assert moved.recap_episode_number_manual_tenths == 55


def test_shared_finalization_cannot_verify_legacy_recap_outside_season():
    collection = CatalogCollection(
        id=1,
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
        hierarchy_status="verified",
        hierarchy_verified_at=utc_now(),
    )
    bonus = CatalogTitle(
        id=2,
        collection=collection,
        local_title="Bonus",
        normalized_local_title="bonus",
        relative_root_path="Anime/Show/Bonus",
    )
    activate_manual_hierarchy_snapshot(
        bonus,
        part_type="bonus",
        season_number=None,
        part_number=None,
        season_label="Bonus",
        sort_order=None,
        verified_at=utc_now(),
    )
    recap = Video(
        id=3,
        relative_path="Anime/Show/Bonus/Recap 3.5.mkv",
        root_folder="Anime",
        filename="Recap 3.5.mkv",
        size=1,
        mtime_ns=1,
        file_type="recap",
        content_type_manual="recap",
        catalog_collection=collection,
        catalog_title=bonus,
    )

    result = finalize_collection_hierarchy(collection, [recap])

    assert result.status == "review_required"
    assert [
        issue.code.value for issue in result.blocking_issues
        if issue.code.value == "recap_outside_season"
    ] == ["recap_outside_season"]
    assert collection.hierarchy_verified_at is None


def test_recap_sort_count_and_presentation_are_numeric_not_lexicographic():
    _collection, title, videos, recaps = _graph(
        (14, 15), recap_positions=("14.5",), expected_count=None,
    )
    ordered, _, _ = sort_title_videos(reversed(videos))
    assert [
        effective_video_content_display(video).noncanonical_position
        or str(video.season_episode_number)
        for video in ordered
    ] == ["14", "14.5", "15"]
    presentation = build_catalog_title_video_presentation(ordered, title)
    assert [row.video.id for row in presentation.display_rows] == [
        _standard_by_number(title, 14).id,
        recaps[0].id,
        _standard_by_number(title, 15).id,
    ]
    summary = summarize_title_numbering(videos, title)
    assert summary.standard_total == 2
    assert summary.resolved_supplemental == 1


def test_slime_24_5_and_24_9_sort_without_count_or_false_proposal():
    _collection, title, videos, _recaps = _graph(
        (24, 25), recap_positions=("24.5", "24.9"), expected_count=None,
    )
    ordered, _, _ = sort_title_videos(reversed(videos))
    assert [
        effective_video_content_display(video).noncanonical_position
        or str(video.season_episode_number)
        for video in ordered
    ] == ["24", "24.5", "24.9", "25"]
    summary = summarize_title_numbering(videos, title)
    assert summary.standard_total == 2
    assert summary.resolved_supplemental == 2
    assert deterministic_bulk_renumber_proposal(title) is None


def test_sao_deterministic_proposal_uses_confirmed_expected_count():
    _collection, title, _videos, _recaps = _graph(
        (*range(1, 15), *range(16, 26)),
    )
    metrics = BulkRenumberMetrics()
    proposal = deterministic_bulk_renumber_proposal(title, metrics=metrics)

    assert proposal is not None
    assert (proposal.gap_start, proposal.gap_end, proposal.offset) == (15, 15, -1)
    assert proposal.expected_episode_count == 24
    assert proposal.expected_count_authoritative is True
    assert [(row.current_episode, row.proposed_episode) for row in proposal.rows] == [
        (number, number - 1) for number in range(16, 26)
    ]
    assert proposal.logical_episode_count == 10
    assert metrics.logical_episodes_scanned == 24
    assert metrics.physical_videos_scanned == 25


def test_local_structure_can_propose_without_unconfirmed_candidate_count():
    _collection, title, _videos, _recaps = _graph(
        (*range(1, 15), *range(16, 26)),
        expected_count=None,
    )
    proposal = deterministic_bulk_renumber_proposal(title)
    assert proposal is not None
    assert proposal.expected_episode_count is None
    assert any("lokální souvislé řady" in warning for warning in proposal.warnings)


def test_only_confirmed_manual_metadata_count_constrains_proposal():
    _collection, title, _videos, _recaps = _graph(
        (*range(1, 15), *range(16, 26)),
        expected_count=None,
    )
    title.metadata_record = TitleMetadata(
        catalog_title_id=title.id,
        display_title="Unconfirmed metadata",
        episode_count=99,
    )
    title.metadata_status = "linked_auto"
    unconfirmed = deterministic_bulk_renumber_proposal(title)
    assert unconfirmed is not None
    assert unconfirmed.expected_episode_count is None

    title.metadata_status = "linked_manual"
    title.external_links.append(ExternalTitleLink(
        provider="anilist",
        external_id="999",
        match_method="manual_search",
        is_primary=True,
        is_manual=True,
        verified_at=utc_now(),
    ))
    assert deterministic_bulk_renumber_proposal(title) is None


@pytest.mark.parametrize(
    "numbers,positions",
    [
        ((*range(1, 15), 16, 18, 19, 22), ("14.5",)),
        ((*range(1, 15), 16, 18, 19, 20), ("14.5",)),
        ((*range(1, 16), *range(17, 26)), ("14.5",)),
    ],
)
def test_ambiguous_multiple_gap_or_collision_shape_has_no_proposal(numbers, positions):
    _collection, title, _videos, _recaps = _graph(
        numbers,
        recap_positions=positions,
        expected_count=None,
    )
    assert deterministic_bulk_renumber_proposal(title) is None


def test_ova_special_bonus_preview_do_not_enter_bulk_rows():
    collection, title, videos, _recaps = _graph(
        (*range(1, 15), *range(16, 26)),
    )
    identifier = max(video.id for video in videos) + 1
    for content_type in ("ova", "special", "bonus", "preview"):
        video = Video(
            id=identifier,
            relative_path=f"{title.relative_root_path}/{content_type} 01.mkv",
            root_folder="Anime",
            filename=f"{content_type} 01.mkv",
            size=identifier,
            mtime_ns=identifier,
            file_type=content_type,
            content_type_manual=content_type,
            catalog_collection=collection,
            catalog_title=title,
        )
        videos.append(video)
        identifier += 1
    recalculate_title_numbering(title, videos)
    proposal = deterministic_bulk_renumber_proposal(title)
    assert proposal is not None
    changed_ids = {
        change.video_id for row in proposal.rows for change in row.physical_changes
    }
    assert not changed_ids & {
        video.id for video in videos if video.content_type_manual not in {None, "recap"}
    }


def test_variants_are_one_logical_row_and_all_physical_representations_change():
    _collection, title, videos, _recaps = _graph(
        (*range(1, 15), *range(16, 26)),
    )
    first_group = VideoVariantGroup(id=100, catalog_title=title, manual_label="TV")
    second_group = VideoVariantGroup(id=200, catalog_title=title, manual_label="BD")
    original = _standard_by_number(title, 16)
    original.video_variant_group = first_group
    original.video_variant_group_id = first_group.id
    variant = Video(
        id=max(video.id for video in videos) + 1,
        relative_path=f"{title.relative_root_path}/Show - 16 Ver.TV.mkv",
        root_folder="Anime",
        filename="Show - 16 Ver.TV.mkv",
        size=100,
        mtime_ns=100,
        file_type="episode",
        catalog_collection=title.collection,
        catalog_title=title,
        video_variant_group=second_group,
        video_variant_group_id=second_group.id,
    )
    videos.append(variant)
    recalculate_title_numbering(title, videos)

    proposal = deterministic_bulk_renumber_proposal(title)
    row = next(row for row in proposal.rows if row.current_episode == 16)
    assert len(row.physical_changes) == 2
    assert len(logical_episode_partitions(videos, catalog_title=title)) == 24


def test_confirmed_duplicate_secondary_is_not_logical_but_changes_with_primary():
    _collection, title, videos, _recaps = _graph(
        (*range(1, 15), *range(16, 26)),
    )
    primary = _standard_by_number(title, 16)
    secondary = Video(
        id=max(video.id for video in videos) + 1,
        relative_path=f"{title.relative_root_path}/copy/Show - 16.mkv",
        root_folder="Anime",
        filename="Show - 16.mkv",
        size=100,
        mtime_ns=100,
        file_type="episode",
        catalog_collection=title.collection,
        catalog_title=title,
        duplicate_of=primary,
        duplicate_of_video_id=primary.id,
    )
    videos.append(secondary)
    recalculate_title_numbering(title, videos)

    proposal = deterministic_bulk_renumber_proposal(title)
    row = next(row for row in proposal.rows if row.current_episode == 16)
    assert len(row.physical_changes) == 2
    assert sum(change.confirmed_duplicate_secondary for change in row.physical_changes) == 1
    assert summarize_title_numbering(videos, title).standard_total == 24


def test_inconsistent_confirmed_copy_collision_blocks_proposal():
    _collection, title, videos, _recaps = _graph(
        (*range(1, 15), *range(16, 26)),
    )
    primary = _standard_by_number(title, 16)
    secondary = Video(
        id=max(video.id for video in videos) + 1,
        relative_path=f"{title.relative_root_path}/copy/Show - 15.mkv",
        root_folder="Anime",
        filename="Show - 15.mkv",
        size=100,
        mtime_ns=100,
        file_type="episode",
        season_episode_number=15,
        catalog_collection=title.collection,
        catalog_title=title,
        duplicate_of=primary,
        duplicate_of_video_id=primary.id,
    )
    videos.append(secondary)

    assert deterministic_bulk_renumber_proposal(title) is None


def test_apply_keeps_variant_representations_and_confirmed_copy_consistent():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, title, videos, _recaps = _graph(
            (*range(1, 15), *range(16, 26)),
            explicit_ids=False,
        )
        recalculate_title_numbering(title, videos)
        tv = VideoVariantGroup(catalog_title=title, manual_label="TV")
        bd = VideoVariantGroup(catalog_title=title, manual_label="BD")
        primary_16 = _standard_by_number(title, 16)
        primary_16.video_variant_group = tv
        variant_16 = Video(
            relative_path=f"{title.relative_root_path}/Show - 16 Ver.TV.mkv",
            root_folder="Anime",
            filename="Show - 16 Ver.TV.mkv",
            size=100,
            mtime_ns=100,
            file_type="episode",
            catalog_collection=collection,
            catalog_title=title,
            video_variant_group=bd,
        )
        primary_17 = _standard_by_number(title, 17)
        copy_17 = Video(
            relative_path=f"{title.relative_root_path}/copy/Show - 17.mkv",
            root_folder="Anime",
            filename="Show - 17.mkv",
            size=101,
            mtime_ns=101,
            file_type="episode",
            catalog_collection=collection,
            catalog_title=title,
            duplicate_of=primary_17,
        )
        recalculate_title_numbering(title, list(title.videos))
        session.add(collection)
        session.flush()
        session.commit()
        title_id = title.id
        variant_ids = (primary_16.id, variant_16.id)
        duplicate_ids = (primary_17.id, copy_17.id)

    with Session(engine) as session:
        title = session.get(CatalogTitle, title_id)
        proposal = deterministic_bulk_renumber_proposal(title)
        assert proposal.logical_episode_count == 10
        apply_deterministic_bulk_renumber(
            session,
            title_id,
            expected_fingerprint=proposal.fingerprint,
        )
        session.commit()

    with Session(engine) as session:
        assert {
            session.get(Video, video_id).season_episode_number
            for video_id in variant_ids
        } == {15}
        assert {
            session.get(Video, video_id).season_episode_number
            for video_id in duplicate_ids
        } == {16}
        copy = session.get(Video, duplicate_ids[1])
        assert copy.duplicate_of_video_id == duplicate_ids[0]


def _persist_sao_graph(engine):
    with Session(engine) as session:
        collection, title, videos, recaps = _graph(
            (*range(1, 15), *range(16, 26)),
            explicit_ids=False,
        )
        session.add(collection)
        session.flush()
        recalculate_title_numbering(title, videos)
        session.commit()
        return title.id, recaps[0].id


def test_apply_is_atomic_revalidates_and_preserves_recap():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    title_id, recap_id = _persist_sao_graph(engine)

    with Session(engine) as session:
        title = session.get(CatalogTitle, title_id)
        proposal = deterministic_bulk_renumber_proposal(title)
        applied = apply_deterministic_bulk_renumber(
            session,
            title_id,
            expected_fingerprint=proposal.fingerprint,
        )
        session.commit()
        assert applied.logical_episode_count == 10

    with Session(engine) as session:
        title = session.get(CatalogTitle, title_id)
        assert [
            partition.identity.season_episode_number
            for partition in logical_episode_partitions(list(title.videos), catalog_title=title)
        ] == list(range(1, 25))
        recap = session.get(Video, recap_id)
        assert manual_recap_episode_number(recap) == Decimal("14.5")
        assert recap.season_episode_number is None


def test_manual_override_requires_extra_confirm_and_stale_preview_changes_nothing():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    title_id, _recap_id = _persist_sao_graph(engine)
    with Session(engine) as session:
        title = session.get(CatalogTitle, title_id)
        video = _standard_by_number(title, 16)
        set_video_episode_override(video, 16)
        recalculate_title_numbering(title, list(title.videos))
        proposal = deterministic_bulk_renumber_proposal(title)
        assert proposal.has_manual_overrides is True
        with pytest.raises(ValueError, match="samostatně potvrdit"):
            apply_deterministic_bulk_renumber(
                session,
                title_id,
                expected_fingerprint=proposal.fingerprint,
            )
        assert video.episode_number_manual_override == 16

        stale_fingerprint = proposal.fingerprint
        set_video_episode_override(video, 99)
        recalculate_title_numbering(title, list(title.videos))
        before = {
            item.id: item.episode_number_manual_override for item in title.videos
        }
        with pytest.raises(ValueError, match="zastaralý"):
            apply_deterministic_bulk_renumber(
                session,
                title_id,
                expected_fingerprint=stale_fingerprint,
                confirm_manual_overrides=True,
            )
        assert before == {
            item.id: item.episode_number_manual_override for item in title.videos
        }


def test_error_mid_apply_rolls_back_every_suffix_change(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    title_id, _recap_id = _persist_sao_graph(engine)
    with Session(engine) as session:
        title = session.get(CatalogTitle, title_id)
        proposal = deterministic_bulk_renumber_proposal(title)
        before = {
            video.id: video.episode_number_manual_override for video in title.videos
        }
        from app import numbering

        original = numbering.set_video_episode_override
        calls = 0

        def fail_on_third(video, value):
            nonlocal calls
            calls += 1
            original(video, value)
            if calls == 3:
                raise RuntimeError("induced failure")

        monkeypatch.setattr(numbering, "set_video_episode_override", fail_on_third)
        with pytest.raises(RuntimeError, match="induced"):
            apply_deterministic_bulk_renumber(
                session,
                title_id,
                expected_fingerprint=proposal.fingerprint,
            )
        session.expire_all()
        title = session.get(CatalogTitle, title_id)
        assert before == {
            video.id: video.episode_number_manual_override for video in title.videos
        }


def test_schema_migration_is_idempotent_and_does_not_backfill_recap_position(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recap-migration.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE videos DROP COLUMN recap_episode_number_manual_tenths"
        ))
    migrate_schema(engine)
    migrate_schema(engine)
    columns = [column["name"] for column in inspect(engine).get_columns("videos")]
    assert columns.count("recap_episode_number_manual_tenths") == 1


def test_server_route_and_both_uis_enforce_dynamic_step_and_render_proposal(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection, title, videos, recaps = _graph(
            (*range(1, 15), *range(16, 26)),
            explicit_ids=False,
        )
        session.add(collection)
        session.flush()
        recalculate_title_numbering(title, videos)
        session.commit()
        collection_id, title_id = collection.id, title.id
        recap_id = recaps[0].id
        standard_id = _standard_by_number(title, 14).id

    endpoints = {
        route.path: route.endpoint
        for route in web_app.routes
        if getattr(route, "path", None) and hasattr(route, "endpoint")
    }
    update = endpoints["/videos/{video_id}/episode-number"]
    with pytest.raises(HTTPException) as exc:
        update(
            standard_id,
            manual_episode_number="14.5",
            filter_name="all",
            q="",
            sort="",
            direction="",
            detail_sort="",
            detail_direction="",
            return_to="",
        )
    assert exc.value.status_code == 400

    update(
        recap_id,
        manual_episode_number="24.9",
        filter_name="all",
        q="",
        sort="",
        direction="",
        detail_sort="",
        detail_direction="",
        return_to="",
    )
    detail = endpoints["/titles/{catalog_title_id}"](
        _request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()
    assert 'step="0.1" inputmode="decimal"' in detail
    assert 'value="24.9"' in detail
    assert 'step="1" inputmode="numeric"' in detail

    # Restore the SAO anchor and verify Hierarchy Review's preview/confirm UI.
    update(
        recap_id,
        manual_episode_number="14.5",
        filter_name="all",
        q="",
        sort="",
        direction="",
        detail_sort="",
        detail_direction="",
        return_to="",
    )
    review = endpoints["/hierarchy-review/{collection_id}"](
        _request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()
    assert "Navržená oprava číslování" in review
    assert "E16" in review and "E15" in review and "E25" in review and "E24" in review
    assert 'name="expected_fingerprint"' in review
    assert 'name="confirm_bulk_renumber"' in review
    assert 'class="inline-form recap-number-form"' in review


def test_parser_fractional_evidence_remains_unchanged():
    detection = detect_episode_number("S01E14.5v2.mkv")
    assert detection.kind == "fractional"
    assert detection.display_value == "14.5"


def test_manual_recap_authority_survives_startup_migration_and_temp_rescan(
    tmp_path: Path,
    monkeypatch,
):
    media = tmp_path / "Show" / "Season 1" / "E15.mkv"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda *_args, **_kwargs: PROBE_RESULT)
    engine = create_engine(f"sqlite:///{tmp_path / 'rescan.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scan_library(session, tmp_path)
        video = session.scalar(select(Video).where(
            Video.relative_path == "Show/Season 1/E15.mkv"
        ))
        video.content_type_manual = "recap"
        set_video_episode_number_from_input(video, "14.5")
        session.commit()
        video_id = video.id

    migrate_schema(engine)
    media.write_bytes(b"video changed")
    with Session(engine) as session:
        scan_library(session, tmp_path)
    with Session(engine) as session:
        video = session.get(Video, video_id)
        assert video.content_type_manual == "recap"
        assert video.recap_episode_number_manual_tenths == 145
        assert manual_recap_episode_number(video) == Decimal("14.5")
        assert video.season_episode_number is None
    assert media.exists()


def test_scanner_and_startup_leave_recap_outside_season_unassigned_for_review(
    tmp_path: Path,
    monkeypatch,
):
    media = tmp_path / "Show" / "Bonus" / "Recap 3.5.mkv"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"video")
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'invalid-recap.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        result = scan_library(session, tmp_path)
        video = session.scalar(select(Video))
        assert result.errors == 0
        assert video.catalog_title_id is None
        assert video.catalog_collection.hierarchy_status == "review_required"
        video_id = video.id

    migrate_schema(engine)

    with Session(engine) as session:
        stored = session.get(Video, video_id)
        assert stored.catalog_title_id is None
        assert stored.catalog_collection.hierarchy_status == "review_required"
        assert stored.catalog_collection.hierarchy_verified_at is None
    assert media.exists()
