import asyncio
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import create_engine, event, inspect, select, text
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
from app.hierarchy_evaluation import (
    evaluate_collection_hierarchy,
    finalize_collection_hierarchy,
)
from app.hierarchy_review import (
    ManualTitleDefinition,
    apply_manual_split,
    classify_videos_in_place,
    clear_confirmed_duplicate_videos,
    confirm_duplicate_videos,
    create_title_from_videos,
    delete_empty_local_title,
)
from app.main import create_app
from app.migrations import migrate_schema
from app.models import (
    CatalogCollection,
    CatalogTitle,
    CollectionGroupingDecision,
    ExternalTitleLink,
    ManualSplitRuleVideo,
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
from app.video_variants import (
    VariantGroupDraft,
    apply_video_variant_assignments,
    preview_video_variant_assignments,
)


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


def _title_hierarchy_endpoint(web_app):
    return next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None)
        == "/collections/{collection_id}/titles/{catalog_title_id}/hierarchy"
    )


def _post_title_hierarchy(
    endpoint,
    collection_id: int,
    title_id: int,
    *,
    part_type: str,
    season_number: str = "",
    season_label: str = "",
):
    return endpoint(
        collection_id,
        title_id,
        season_number_manual=season_number,
        season_label_manual=season_label,
        part_number_manual="",
        part_type_manual=part_type,
        sort_order_manual="",
        hierarchy_verified=True,
        filter_name="all",
        q="",
        sort="",
        direction="",
        return_to="hierarchy_review",
    )


def _hierarchy_write_state(session: Session, collection_id: int) -> tuple:
    def stored_datetime(value):
        return value.replace(tzinfo=None) if value is not None else None

    collection = session.get(CatalogCollection, collection_id)
    return (
        (
            collection.hierarchy_status,
            stored_datetime(collection.hierarchy_verified_at),
            collection.hierarchy_note,
            stored_datetime(collection.created_at),
            stored_datetime(collection.updated_at),
        ),
        tuple(sorted(
            (
                title.id,
                title.part_type,
                title.season_number,
                title.part_number,
                title.season_label,
                title.sort_order,
                title.hierarchy_manual_override,
                title.part_type_manual,
                title.season_number_manual,
                title.part_number_manual,
                title.season_label_manual,
                title.sort_order_manual,
                stored_datetime(title.hierarchy_verified_at),
                stored_datetime(title.created_at),
                stored_datetime(title.updated_at),
            )
            for title in collection.titles
        )),
        tuple(sorted(
            (
                video.id,
                video.catalog_title_id,
                video.catalog_collection_id,
                video.content_type_manual,
                video.recap_episode_number_manual_tenths,
                video.episode_number_manual_override,
                stored_datetime(video.episode_number_verified_at),
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.external_episode_number,
                video.episode_number_source,
                video.episode_number_confidence,
                video.file_type,
                video.filename,
                video.relative_path,
                video.size,
                video.mtime_ns,
            )
            for video in collection.videos
        )),
    )


def _all_structural_write_state(session: Session) -> tuple:
    def stored_datetime(value):
        return value.replace(tzinfo=None) if value is not None else None

    return (
        tuple(sorted(
            (
                collection.id,
                collection.hierarchy_status,
                collection.hierarchy_note,
                stored_datetime(collection.hierarchy_verified_at),
                stored_datetime(collection.updated_at),
            )
            for collection in session.scalars(select(CatalogCollection))
        )),
        tuple(sorted(
            (
                title.id,
                title.catalog_collection_id,
                title.part_type,
                title.season_number,
                title.part_number,
                title.season_label,
                title.sort_order,
                title.hierarchy_manual_override,
                title.part_type_manual,
                title.season_number_manual,
                title.part_number_manual,
                title.season_label_manual,
                title.sort_order_manual,
                stored_datetime(title.hierarchy_verified_at),
                title.episode_start,
                title.episode_end,
                title.episode_start_offset,
                title.numbering_mode,
                title.numbering_manual,
                stored_datetime(title.numbering_verified_at),
                title.episode_filename_pattern,
                stored_datetime(title.updated_at),
            )
            for title in session.scalars(select(CatalogTitle))
        )),
        tuple(sorted(
            (
                video.id,
                video.catalog_collection_id,
                video.catalog_title_id,
                video.file_type,
                video.content_type_manual,
                video.recap_episode_number_manual_tenths,
                video.episode_number_manual_override,
                stored_datetime(video.episode_number_verified_at),
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.external_episode_number,
                video.episode_number_source,
                video.episode_number_confidence,
                video.media_part_number,
                video.duplicate_status_manual,
                video.duplicate_of_video_id,
                video.duplicate_primary_missing,
                video.video_variant_group_id,
                video.filename,
                video.relative_path,
            )
            for video in session.scalars(select(Video))
        )),
        tuple(sorted(session.execute(select(
            ManualSplitRuleVideo.catalog_title_id,
            ManualSplitRuleVideo.video_id,
        )).all())),
        tuple(sorted(
            (
                decision.id,
                decision.suggestion_key,
                decision.state_fingerprint,
                decision.decision,
                decision.target_collection_path,
                decision.selected_title_paths_json,
                stored_datetime(decision.updated_at),
            )
            for decision in session.scalars(select(CollectionGroupingDecision))
        )),
    )


def _route_endpoint(web_app, path: str):
    return next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == path
    )


def _public_manage_videos(web_app, collection_id: int, items):
    return asyncio.run(_route_endpoint(
        web_app,
        "/hierarchy-review/{collection_id}/manage-videos",
    )(
        _post_form_request(
            web_app,
            f"/hierarchy-review/{collection_id}/manage-videos",
            items,
        ),
        collection_id,
    ))


def _public_set_episode_number(web_app, video_id: int, value: str):
    return _route_endpoint(web_app, "/videos/{video_id}/episode-number")(
        video_id,
        manual_episode_number=value,
        filter_name="all",
        q="",
        sort="",
        direction="",
        detail_sort="",
        detail_direction="",
        return_to="",
    )


def _seed_r1_automatic_recap_app(
    tmp_path: Path,
    monkeypatch,
    *,
    extra_filenames: tuple[str, ...] = (),
    existing_bonus: bool = False,
    manual_recap: bool = False,
):
    """Scan the public R1 graph, then explicitly attach its Recap to auto S1."""
    library = tmp_path / "library"
    root = library / "Show"
    root.mkdir(parents=True)
    filenames = ["E01.mkv", "E02.mkv", "Recap 3.5.mkv", *extra_filenames]
    if existing_bonus:
        filenames.append("Bonus 01.mkv")
    for filename in filenames:
        (root / filename).write_bytes(b"video")
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    web_app = create_app(Settings(
        anime_path=library,
        database_url=f"sqlite:///{tmp_path / 'r1.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        scan_library(session, library)
        collection = session.scalar(select(CatalogCollection))
        season = next(
            title for title in collection.titles
            if title.effective_part_type == "season"
        )
        videos = {video.filename: video for video in collection.videos}
        recap = videos["Recap 3.5.mkv"]
        assert season.effective_season_number == 1
        assert recap.catalog_title_id is None
        ids = {
            "collection": collection.id,
            "season": season.id,
            "e01": videos["E01.mkv"].id,
            "e02": videos["E02.mkv"].id,
            "recap": recap.id,
        }
        for filename in extra_filenames:
            ids[filename] = videos[filename].id
        bonus_video_id = videos["Bonus 01.mkv"].id if existing_bonus else None

    if existing_bonus:
        response = _public_manage_videos(web_app, ids["collection"], [
            ("video_ids", str(bonus_video_id)),
            ("operation", "create"),
            ("local_title", "Bonus"),
            ("part_type", "bonus"),
            ("season_number", ""),
            ("season_label", ""),
            ("part_number", ""),
            ("sort_order", ""),
        ])
        assert response.status_code == 303
        with web_app.state.sessions() as session:
            collection = session.get(CatalogCollection, ids["collection"])
            ids["bonus"] = next(
                title.id for title in collection.titles
                if title.effective_part_type == "bonus"
            )

    response = _route_endpoint(
        web_app,
        "/root-videos/{video_id}/assignment",
    )(
        ids["recap"],
        target_title_id=str(ids["season"]),
        confirm_manual=True,
    )
    assert response.status_code == 303

    if manual_recap:
        assert _public_classify_video(
            web_app,
            ids["collection"],
            ids["recap"],
            "recap",
        ).status_code == 303
        assert _public_set_episode_number(
            web_app, ids["recap"], "24.9"
        ).status_code == 303

    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, ids["collection"])
        season = session.get(CatalogTitle, ids["season"])
        recap = session.get(Video, ids["recap"])
        assert season.effective_part_type == "season"
        assert season.effective_season_number == 1
        assert recap.catalog_title_id == season.id
        assert not evaluate_collection_hierarchy(collection).blocking_issues
        if manual_recap:
            assert recap.content_type_manual == "recap"
            assert effective_recap_episode_number(recap) == Decimal("24.9")
        else:
            assert recap.content_type_manual is None
            assert effective_recap_episode_number(recap) == Decimal("3.5")
        before = _all_structural_write_state(session)
    return web_app, ids, before


@pytest.mark.parametrize("target_mode", ["new", "existing"])
def test_r1_public_episode_move_rejects_post_finalization_recap_orphan_atomically(
    tmp_path: Path,
    monkeypatch,
    target_mode: str,
):
    web_app, ids, before = _seed_r1_automatic_recap_app(
        tmp_path,
        monkeypatch,
        existing_bonus=target_mode == "existing",
    )
    items = [("video_ids", str(ids["e02"]))]
    if target_mode == "new":
        items.extend((
            ("operation", "create"),
            ("local_title", "Bonus"),
            ("part_type", "bonus"),
            ("season_number", ""),
            ("season_label", ""),
            ("part_number", ""),
            ("sort_order", ""),
        ))
    else:
        items.extend((
            ("operation", "move"),
            ("target_title_id", str(ids["bonus"])),
        ))

    with pytest.raises(HTTPException) as raised:
        _public_manage_videos(web_app, ids["collection"], items)

    assert raised.value.status_code == 400
    assert "Season kontextu" in raised.value.detail
    with web_app.state.sessions() as session:
        assert _all_structural_write_state(session) == before
        collection = session.get(CatalogCollection, ids["collection"])
        season = session.get(CatalogTitle, ids["season"])
        assert season.effective_part_type == "season"
        assert session.get(Video, ids["e02"]).catalog_title_id == season.id
        assert session.get(Video, ids["recap"]).catalog_title_id == season.id
        if target_mode == "new":
            assert len(collection.titles) == 1


def test_r1_public_episode_move_rejects_manual_24_9_recap_atomically(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, before = _seed_r1_automatic_recap_app(
        tmp_path,
        monkeypatch,
        manual_recap=True,
    )

    with pytest.raises(HTTPException, match="Season kontextu"):
        _public_manage_videos(web_app, ids["collection"], [
            ("video_ids", str(ids["e02"])),
            ("operation", "create"),
            ("local_title", "Bonus"),
            ("part_type", "bonus"),
            ("season_number", ""),
            ("season_label", ""),
            ("part_number", ""),
            ("sort_order", ""),
        ])

    with web_app.state.sessions() as session:
        assert _all_structural_write_state(session) == before
        recap = session.get(Video, ids["recap"])
        assert recap.content_type_manual == "recap"
        assert effective_recap_episode_number(recap) == Decimal("24.9")


def test_r1_manual_split_rejects_source_demotion_after_finalization(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, before = _seed_r1_automatic_recap_app(tmp_path, monkeypatch)
    with web_app.state.sessions() as session:
        season = session.get(CatalogTitle, ids["season"])
        definitions = [
            ManualTitleDefinition(
                title_id=season.id,
                local_title=season.local_title,
                manual_display_title=None,
                season_number_manual=None,
                season_label_manual=None,
                part_number_manual=None,
                part_type_manual=None,
                episode_start=None,
                episode_end=None,
                episode_start_offset=None,
                numbering_mode=season.numbering_mode,
                sort_order=None,
                filename_pattern=None,
                video_ids=(ids["e01"], ids["recap"]),
            ),
            ManualTitleDefinition(
                title_id=None,
                local_title="Bonus",
                manual_display_title=None,
                season_number_manual=None,
                season_label_manual=None,
                part_number_manual=None,
                part_type_manual="bonus",
                episode_start=None,
                episode_end=None,
                episode_start_offset=None,
                numbering_mode="unknown",
                sort_order=None,
                filename_pattern=None,
                video_ids=(ids["e02"],),
            ),
        ]

        with pytest.raises(ValueError, match="Season kontextu"):
            apply_manual_split(session, ids["collection"], definitions)

        assert _all_structural_write_state(session) == before
        assert not session.new
        assert not session.deleted
        assert not session.dirty
        assert session.get(Video, ids["e02"]).catalog_title_id == ids["season"]


def test_r1_classifying_other_episode_rejects_post_finalization_recap_orphan(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, before = _seed_r1_automatic_recap_app(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as raised:
        _public_classify_video(
            web_app, ids["collection"], ids["e02"], "bonus"
        )

    assert raised.value.status_code == 400
    assert "Season kontextu" in raised.value.detail
    with web_app.state.sessions() as session:
        assert _all_structural_write_state(session) == before
        assert session.get(Video, ids["e02"]).content_type_manual is None


def test_r1_renumbering_other_episode_rejects_post_finalization_recap_orphan(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, before = _seed_r1_automatic_recap_app(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as raised:
        _public_set_episode_number(web_app, ids["e02"], "3")

    assert raised.value.status_code == 400
    assert "Season kontextu" in raised.value.detail
    with web_app.state.sessions() as session:
        assert _all_structural_write_state(session) == before
        episode = session.get(Video, ids["e02"])
        assert episode.episode_number_manual_override is None
        assert episode.season_episode_number == 2


def test_r1_complete_manual_season_allows_episode_move_that_keeps_recap_context(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, _before = _seed_r1_automatic_recap_app(tmp_path, monkeypatch)
    assert _post_confirm_part(
        web_app, ids["collection"], part_type="season"
    ).status_code == 303

    response = _public_manage_videos(web_app, ids["collection"], [
        ("video_ids", str(ids["e02"])),
        ("operation", "create"),
        ("local_title", "Bonus"),
        ("part_type", "bonus"),
        ("season_number", ""),
        ("season_label", ""),
        ("part_number", ""),
        ("sort_order", ""),
    ])

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        season = session.get(CatalogTitle, ids["season"])
        recap = session.get(Video, ids["recap"])
        assert season.hierarchy_manual_override is True
        assert season.effective_part_type == "season"
        assert recap.catalog_title_id == season.id


def test_r1_write_is_allowed_when_automatic_season_survives_finalization(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, _before = _seed_r1_automatic_recap_app(
        tmp_path,
        monkeypatch,
        extra_filenames=("E03.mkv",),
    )

    response = _public_manage_videos(web_app, ids["collection"], [
        ("video_ids", str(ids["E03.mkv"])),
        ("operation", "create"),
        ("local_title", "Bonus"),
        ("part_type", "bonus"),
        ("season_number", ""),
        ("season_label", ""),
        ("part_number", ""),
        ("sort_order", ""),
    ])

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        season = session.get(CatalogTitle, ids["season"])
        assert season.effective_part_type == "season"
        assert session.get(Video, ids["recap"]).catalog_title_id == season.id
        assert not evaluate_collection_hierarchy(season.collection).blocking_issues


def test_r1_raw_recap_classified_bonus_is_not_a_recap_dependency(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, _before = _seed_r1_automatic_recap_app(tmp_path, monkeypatch)
    assert _public_classify_video(
        web_app, ids["collection"], ids["recap"], "bonus"
    ).status_code == 303

    response = _public_manage_videos(web_app, ids["collection"], [
        ("video_ids", str(ids["e02"])),
        ("operation", "create"),
        ("local_title", "Bonus 2"),
        ("part_type", "bonus"),
        ("season_number", ""),
        ("season_label", ""),
        ("part_number", ""),
        ("sort_order", ""),
    ])

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        recap = session.get(Video, ids["recap"])
        assert recap.file_type == "recap"
        assert recap.content_type_manual == "bonus"
        assert effective_video_content_type(recap) == "bonus"
        assert effective_recap_episode_number(recap) is None


def test_r1_preexisting_legacy_recap_violation_does_not_block_unrelated_write(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, _before = _seed_r1_automatic_recap_app(
        tmp_path,
        monkeypatch,
        extra_filenames=("E03.mkv",),
    )
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, ids["collection"])
        legacy_title = CatalogTitle(
            collection=collection,
            local_title="Legacy Extras",
            normalized_local_title="legacy extras",
            relative_root_path="Show/.legacy-extras",
            part_type="bonus",
        )
        activate_manual_hierarchy_snapshot(
            legacy_title,
            part_type="bonus",
            season_number=None,
            part_number=None,
            season_label=None,
            sort_order=None,
            verified_at=utc_now(),
        )
        legacy_recap = Video(
            relative_path="Show/.legacy-extras/Recap 8.5.mkv",
            root_folder="Show",
            filename="Recap 8.5.mkv",
            size=99,
            mtime_ns=99,
            file_type="recap",
            catalog_collection=collection,
            catalog_title=legacy_title,
        )
        session.add_all([legacy_title, legacy_recap])
        finalize_collection_hierarchy(collection)
        session.commit()
        legacy_id = legacy_recap.id
        assert any(
            issue.code.value == "recap_outside_season"
            for issue in evaluate_collection_hierarchy(collection).issues
        )

    response = _public_manage_videos(web_app, ids["collection"], [
        ("video_ids", str(ids["E03.mkv"])),
        ("operation", "create"),
        ("local_title", "Bonus"),
        ("part_type", "bonus"),
        ("season_number", ""),
        ("season_label", ""),
        ("part_number", ""),
        ("sort_order", ""),
    ])

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        legacy = session.get(Video, legacy_id)
        season = session.get(CatalogTitle, ids["season"])
        assert legacy.catalog_title.effective_part_type == "bonus"
        assert effective_video_content_type(legacy) == "recap"
        assert season.effective_part_type == "season"
        assert session.get(Video, ids["recap"]).catalog_title_id == season.id


def _seed_r1_repeated_episode_authority_app(
    tmp_path: Path,
    monkeypatch,
    *,
    authority: str,
):
    library = tmp_path / "library"
    root = library / "Show"
    root.mkdir(parents=True)
    filenames = (
        (
            "Show - 01.mkv",
            "Show - 01 Ver.TV.mkv",
            "Show - 02.mkv",
            "Show - 02 Ver.TV.mkv",
            "Recap 3.5.mkv",
        )
        if authority == "variant"
        else (
            "Show - 01.mkv",
            "Show 01.mp4",
            "Show - 02.mkv",
            "Recap 3.5.mkv",
        )
    )
    for filename in filenames:
        (root / filename).write_bytes(b"video")
    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: PROBE_RESULT,
    )
    web_app = create_app(Settings(
        anime_path=library,
        database_url=f"sqlite:///{tmp_path / f'r1-{authority}.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        scan_library(session, library)
        collection = session.scalar(select(CatalogCollection))
        title, = collection.titles
        videos = {video.filename: video for video in collection.videos}
        recap = videos["Recap 3.5.mkv"]
        standard = [video for video in title.videos if video.id != recap.id]
        if authority == "variant":
            assignments = tuple(
                (
                    video.id,
                    "tv" if "Ver.TV" in video.filename else "base",
                )
                for video in standard
            )
            drafts = (
                VariantGroupDraft(key="base", manual_label="Base"),
                VariantGroupDraft(
                    key="tv", manual_label="TV", release_source="tv"
                ),
            )
            preview = preview_video_variant_assignments(
                session,
                collection.id,
                title.id,
                assignments=assignments,
                drafts=drafts,
            )
            apply_video_variant_assignments(
                session,
                collection.id,
                title.id,
                assignments=assignments,
                drafts=drafts,
                expected_fingerprint=preview.fingerprint,
            )
            authority_ids = tuple(video.id for video in standard)
        else:
            primary = videos["Show - 01.mkv"]
            secondary = videos["Show 01.mp4"]
            confirm_duplicate_videos(
                session,
                collection.id,
                [primary.id, secondary.id],
                primary.id,
            )
            authority_ids = (primary.id, secondary.id)
        session.commit()
        assert title.effective_part_type == "season"
        ids = {
            "collection": collection.id,
            "season": title.id,
            "recap": recap.id,
            "authority": authority_ids,
        }

    response = _route_endpoint(
        web_app,
        "/root-videos/{video_id}/assignment",
    )(
        ids["recap"],
        target_title_id=str(ids["season"]),
        confirm_manual=True,
    )
    assert response.status_code == 303
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, ids["collection"])
        assert session.get(CatalogTitle, ids["season"]).effective_part_type == "season"
        assert not evaluate_collection_hierarchy(collection).blocking_issues
        before = _all_structural_write_state(session)
    return web_app, ids, before


def test_r1_clearing_variant_authority_rejects_recap_orphan_atomically(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, before = _seed_r1_repeated_episode_authority_app(
        tmp_path,
        monkeypatch,
        authority="variant",
    )
    with web_app.state.sessions() as session:
        cleared_video_id = ids["authority"][0]
        preview = preview_video_variant_assignments(
            session,
            ids["collection"],
            ids["season"],
            assignments=((cleared_video_id, "null"),),
            drafts=(),
        )

        with pytest.raises(ValueError, match="Season kontextu"):
            apply_video_variant_assignments(
                session,
                ids["collection"],
                ids["season"],
                assignments=((cleared_video_id, "null"),),
                drafts=(),
                expected_fingerprint=preview.fingerprint,
            )

        assert _all_structural_write_state(session) == before
        assert not session.new
        assert not session.deleted
        assert not session.dirty


def test_r1_clearing_duplicate_authority_rejects_recap_orphan_atomically(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, before = _seed_r1_repeated_episode_authority_app(
        tmp_path,
        monkeypatch,
        authority="duplicate",
    )
    with web_app.state.sessions() as session:
        with pytest.raises(ValueError, match="Season kontextu"):
            clear_confirmed_duplicate_videos(
                session,
                ids["collection"],
                list(ids["authority"]),
            )

        assert _all_structural_write_state(session) == before
        assert not session.new
        assert not session.deleted
        assert not session.dirty


def test_r1_reset_manual_season_rejects_automatic_recap_orphan_atomically(
    tmp_path: Path,
    monkeypatch,
):
    web_app, ids, _before = _seed_r1_automatic_recap_app(tmp_path, monkeypatch)
    assert _post_confirm_part(
        web_app, ids["collection"], part_type="season"
    ).status_code == 303
    assert _public_manage_videos(web_app, ids["collection"], [
        ("video_ids", str(ids["e02"])),
        ("operation", "create"),
        ("local_title", "Bonus"),
        ("part_type", "bonus"),
        ("season_number", ""),
        ("season_label", ""),
        ("part_number", ""),
        ("sort_order", ""),
    ]).status_code == 303
    with web_app.state.sessions() as session:
        before = _all_structural_write_state(session)
        season = session.get(CatalogTitle, ids["season"])
        verified_at = season.hierarchy_verified_at
        assert season.hierarchy_manual_override is True
        assert season.effective_part_type == "season"

    endpoint = _title_hierarchy_endpoint(web_app)
    with pytest.raises(HTTPException) as raised:
        endpoint(
            ids["collection"],
            ids["season"],
            season_number_manual="",
            season_label_manual="",
            part_number_manual="",
            part_type_manual="",
            sort_order_manual="",
            hierarchy_verified=False,
            filter_name="all",
            q="",
            sort="",
            direction="",
            return_to="hierarchy_review",
        )

    assert raised.value.status_code == 400
    assert "Season kontextu" in raised.value.detail
    with web_app.state.sessions() as session:
        assert _all_structural_write_state(session) == before
        season = session.get(CatalogTitle, ids["season"])
        assert season.hierarchy_manual_override is True
        assert season.hierarchy_verified_at == verified_at
        assert season.effective_part_type == "season"


def test_r1_post_finalization_guard_query_count_is_bounded_by_video_rows(
    tmp_path: Path,
    monkeypatch,
):
    def statement_count(label: str, extra_count: int) -> int:
        case_path = tmp_path / label
        case_path.mkdir()
        web_app, ids, _before = _seed_r1_automatic_recap_app(
            case_path,
            monkeypatch,
            extra_filenames=("E03.mkv",),
        )
        with web_app.state.sessions() as session:
            collection = session.get(CatalogCollection, ids["collection"])
            season = session.get(CatalogTitle, ids["season"])
            for index in range(1, extra_count + 1):
                session.add(Video(
                    relative_path=f"Show/Bonus extra {index:02}.mkv",
                    root_folder="Show",
                    filename=f"Bonus extra {index:02}.mkv",
                    size=100 + index,
                    mtime_ns=100 + index,
                    file_type="bonus",
                    content_type_manual="bonus",
                    catalog_collection=collection,
                    catalog_title=season,
                ))
            finalize_collection_hierarchy(collection)
            session.commit()
            engine = session.get_bind()

        statements = 0

        def count_statement(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", count_statement)
        try:
            response = _public_classify_video(
                web_app,
                ids["collection"],
                ids["E03.mkv"],
                "bonus",
            )
            assert response.status_code == 303
        finally:
            event.remove(engine, "before_cursor_execute", count_statement)
        return statements

    assert statement_count("one", 1) == statement_count("many", 40)


def _seed_recap_title_hierarchy_app(
    tmp_path: Path,
    *,
    manual_recap: bool,
):
    database_name = "manual-recap-title.db" if manual_recap else "parser-recap-title.db"
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / database_name}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
            hierarchy_status="verified",
            hierarchy_verified_at=utc_now(),
        )
        season = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1",
            part_type="season",
            season_number=1,
            season_label="S1",
        )
        activate_manual_hierarchy_snapshot(
            season,
            part_type="season",
            season_number=1,
            part_number=None,
            season_label="S1",
            sort_order=None,
            verified_at=utc_now(),
        )
        recap = Video(
            relative_path="Anime/Show/Season 1/Recap 3.5.mkv",
            root_folder="Anime",
            filename="Recap 3.5.mkv",
            size=1,
            mtime_ns=1,
            file_type="recap",
            content_type_manual="recap" if manual_recap else None,
            recap_episode_number_manual_tenths=249 if manual_recap else None,
            catalog_collection=collection,
            catalog_title=season,
        )
        Video(
            relative_path="Anime/Show/Season 1/Episode 01.mkv",
            root_folder="Anime",
            filename="Episode 01.mkv",
            size=2,
            mtime_ns=2,
            file_type="episode",
            season_episode_number=1,
            catalog_collection=collection,
            catalog_title=season,
        )
        Video(
            relative_path="Anime/Show/Season 1/Bonus interview.mkv",
            root_folder="Anime",
            filename="Bonus interview.mkv",
            size=3,
            mtime_ns=3,
            file_type="bonus",
            catalog_collection=collection,
            catalog_title=season,
        )
        session.add(collection)
        session.commit()
        ids = collection.id, season.id, recap.id
        before = _hierarchy_write_state(session, collection.id)
    return web_app, ids, before


def _seed_single_title_confirmation_app(
    tmp_path: Path,
    *,
    recap_mode: str,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / f'confirm-{recap_mode}.db'}",
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
        title = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1",
            part_type="season",
            season_number=1,
            season_label="S1",
        )
        for number in (1, 2):
            Video(
                relative_path=f"Anime/Show/Season 1/E{number:02}.mkv",
                root_folder="Anime",
                filename=f"E{number:02}.mkv",
                size=number,
                mtime_ns=number,
                file_type="episode",
                catalog_collection=collection,
                catalog_title=title,
            )
        recap = None
        if recap_mode != "none":
            recap = Video(
                relative_path="Anime/Show/Season 1/Recap 3.5.mkv",
                root_folder="Anime",
                filename="Recap 3.5.mkv",
                size=3,
                mtime_ns=3,
                file_type="recap",
                content_type_manual="recap" if recap_mode == "manual" else None,
                recap_episode_number_manual_tenths=(
                    249 if recap_mode == "manual" else None
                ),
                catalog_collection=collection,
                catalog_title=title,
            )
        session.add(collection)
        session.flush()
        finalize_collection_hierarchy(collection)
        session.commit()
        assert collection.hierarchy_status == "automatic"
        ids = collection.id, title.id, recap.id if recap is not None else None
        before = _all_structural_write_state(session)
    return web_app, ids, before


def _confirm_part_endpoint(web_app):
    return next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None)
        == "/hierarchy-review/{collection_id}/confirm-part"
    )


def _post_confirm_part(web_app, collection_id: int, *, part_type: str):
    return _confirm_part_endpoint(web_app)(
        collection_id,
        part_type_manual=part_type,
        season_number_manual="1" if part_type == "season" else "",
        season_label_manual="S1" if part_type == "season" else "",
        part_number_manual="",
        confirm_part=True,
    )


def _public_classify_video(web_app, collection_id: int, video_id: int, content_type: str):
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None)
        == "/hierarchy-review/{collection_id}/manage-videos"
    )
    return asyncio.run(endpoint(
        _post_form_request(
            web_app,
            f"/hierarchy-review/{collection_id}/manage-videos",
            [
                ("video_ids", str(video_id)),
                ("operation", "classify"),
                ("content_type", content_type),
            ],
        ),
        collection_id,
    ))


@pytest.mark.parametrize("recap_mode", ["parser", "manual"])
def test_public_confirm_part_rejects_nonseason_effective_recap_atomically(
    tmp_path: Path,
    recap_mode: str,
):
    web_app, (collection_id, title_id, recap_id), before = (
        _seed_single_title_confirmation_app(tmp_path, recap_mode=recap_mode)
    )

    with pytest.raises(HTTPException) as raised:
        _post_confirm_part(web_app, collection_id, part_type="bonus")

    assert raised.value.status_code == 400
    assert "efektivní Recap" in raised.value.detail
    assert "Season kontextu" in raised.value.detail
    with web_app.state.sessions() as session:
        assert _all_structural_write_state(session) == before
        title = session.get(CatalogTitle, title_id)
        recap = session.get(Video, recap_id)
        assert title.hierarchy_manual_override is False
        assert title.part_type_manual is None
        assert recap.file_type == "recap"
        assert detect_episode_number(recap.filename).display_value == "3.5"
        if recap_mode == "manual":
            assert recap.content_type_manual == "recap"
            assert effective_recap_episode_number(recap) == Decimal("24.9")
        else:
            assert recap.content_type_manual is None
            assert effective_recap_episode_number(recap) == Decimal("3.5")


@pytest.mark.parametrize(
    ("recap_mode", "preparation", "part_type"),
    (
        ("parser", "none", "season"),
        ("parser", "classify_bonus", "bonus"),
        ("manual", "clear_and_classify_bonus", "bonus"),
        ("none", "none", "bonus"),
    ),
)
def test_public_confirm_part_allows_valid_prospective_structure(
    tmp_path: Path,
    recap_mode: str,
    preparation: str,
    part_type: str,
):
    web_app, (collection_id, title_id, recap_id), _before = (
        _seed_single_title_confirmation_app(tmp_path, recap_mode=recap_mode)
    )
    if preparation == "classify_bonus":
        assert _public_classify_video(
            web_app, collection_id, recap_id, "bonus"
        ).status_code == 303
    elif preparation == "clear_and_classify_bonus":
        endpoint = next(
            route.endpoint for route in web_app.routes
            if getattr(route, "path", None) == "/videos/{video_id}/episode-number"
        )
        endpoint(
            recap_id,
            manual_episode_number="",
            filter_name="all",
            q="",
            sort="",
            direction="",
            detail_sort="",
            detail_direction="",
            return_to="",
        )
        assert _public_classify_video(
            web_app, collection_id, recap_id, "bonus"
        ).status_code == 303

    response = _post_confirm_part(web_app, collection_id, part_type=part_type)

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        title = session.get(CatalogTitle, title_id)
        assert title.hierarchy_manual_override is True
        assert title.part_type_manual == part_type
        assert title.hierarchy_verified_at is not None
        if recap_id is not None:
            recap = session.get(Video, recap_id)
            assert recap.file_type == "recap"
            if part_type == "season":
                assert recap.content_type_manual is None
                assert effective_recap_episode_number(recap) == Decimal("3.5")
            else:
                assert recap.content_type_manual == "bonus"
                assert effective_recap_episode_number(recap) is None


def _seed_collection_move_app(
    tmp_path: Path,
    *,
    recap_mode: str,
    include_unrelated: bool = False,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / f'move-{recap_mode}.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        source = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        target = CatalogCollection(
            local_title="Other",
            normalized_local_title="other",
            relative_root_path="Anime/Other",
        )
        season = CatalogTitle(
            collection=source,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1",
            part_type="season",
            season_number=1,
            season_label="S1",
        )
        extras = CatalogTitle(
            collection=source,
            local_title="Season 1 Extras",
            normalized_local_title="season 1 extras",
            relative_root_path="Anime/Show/Season 1 Extras",
            part_type="bonus",
            season_number=1,
            season_label="S1",
        )
        season_two = CatalogTitle(
            collection=target,
            local_title="Season 2",
            normalized_local_title="season 2",
            relative_root_path="Anime/Other/Season 2",
            part_type="season",
            season_number=2,
            season_label="S2",
        )
        for title, part_type, season_number in (
            (season, "season", 1),
            (extras, "bonus", 1),
            (season_two, "season", 2),
        ):
            activate_manual_hierarchy_snapshot(
                title,
                part_type=part_type,
                season_number=season_number,
                part_number=None,
                season_label=f"S{season_number}",
                sort_order=None,
                verified_at=utc_now(),
            )
        for number in (1, 2):
            Video(
                relative_path=f"Anime/Show/Season 1/E{number:02}.mkv",
                root_folder="Anime",
                filename=f"E{number:02}.mkv",
                size=number,
                mtime_ns=number,
                file_type="episode",
                catalog_collection=source,
                catalog_title=season,
            )
        recap = Video(
            relative_path="Anime/Show/Season 1 Extras/Recap 3.5.mkv",
            root_folder="Anime",
            filename="Recap 3.5.mkv",
            size=3,
            mtime_ns=3,
            file_type="recap",
            content_type_manual=(
                "recap" if recap_mode == "manual" else
                "bonus" if recap_mode == "bonus" else None
            ),
            recap_episode_number_manual_tenths=(
                249 if recap_mode == "manual" else None
            ),
            catalog_collection=source,
            catalog_title=extras,
        )
        Video(
            relative_path="Anime/Other/Season 2/E01.mkv",
            root_folder="Anime",
            filename="E01.mkv",
            size=4,
            mtime_ns=4,
            file_type="episode",
            catalog_collection=target,
            catalog_title=season_two,
        )
        unrelated = None
        if include_unrelated:
            unrelated = CatalogTitle(
                collection=source,
                local_title="Film",
                normalized_local_title="film",
                relative_root_path="Anime/Show/Film",
                part_type="film",
            )
            activate_manual_hierarchy_snapshot(
                unrelated,
                part_type="film",
                season_number=None,
                part_number=None,
                season_label=None,
                sort_order=None,
                verified_at=utc_now(),
            )
            Video(
                relative_path="Anime/Show/Film/Movie.mkv",
                root_folder="Anime",
                filename="Movie.mkv",
                size=5,
                mtime_ns=5,
                file_type="film",
                catalog_collection=source,
                catalog_title=unrelated,
            )
        session.add_all([source, target])
        session.flush()
        finalize_collection_hierarchy(source)
        finalize_collection_hierarchy(target)
        session.commit()
        assert source.hierarchy_status == "verified"
        assert target.hierarchy_status == "verified"
        ids = {
            "source": source.id,
            "target": target.id,
            "season": season.id,
            "extras": extras.id,
            "season_two": season_two.id,
            "recap": recap.id,
            "unrelated": unrelated.id if unrelated is not None else None,
        }
        before = _all_structural_write_state(session)
    return web_app, ids, before


def _public_move_titles(web_app, target_id: int, title_ids: list[int]):
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/collections/move"
    )
    return asyncio.run(endpoint(_post_form_request(
        web_app,
        "/hierarchy-review/collections/move",
        [("title_ids", str(title_id)) for title_id in title_ids]
        + [("target_collection_id", str(target_id))],
    )))


@pytest.mark.parametrize("recap_mode", ["parser", "manual"])
def test_public_collection_move_rejects_orphaned_supplementary_recap_atomically(
    tmp_path: Path,
    recap_mode: str,
):
    web_app, ids, before = _seed_collection_move_app(
        tmp_path,
        recap_mode=recap_mode,
    )

    with pytest.raises(HTTPException) as raised:
        _public_move_titles(web_app, ids["target"], [ids["season"]])

    assert raised.value.status_code == 400
    assert "efektivní Recap" in raised.value.detail
    assert "Season kontextu" in raised.value.detail
    with web_app.state.sessions() as session:
        assert _all_structural_write_state(session) == before
        recap = session.get(Video, ids["recap"])
        assert recap.catalog_collection_id == ids["source"]
        assert recap.catalog_title_id == ids["extras"]
        assert recap.file_type == "recap"
        if recap_mode == "manual":
            assert recap.content_type_manual == "recap"
            assert effective_recap_episode_number(recap) == Decimal("24.9")
        else:
            assert recap.content_type_manual is None
            assert effective_recap_episode_number(recap) == Decimal("3.5")


def test_public_collection_move_allows_raw_recap_classified_bonus(
    tmp_path: Path,
):
    web_app, ids, _before = _seed_collection_move_app(
        tmp_path,
        recap_mode="bonus",
    )

    response = _public_move_titles(web_app, ids["target"], [ids["season"]])

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        recap = session.get(Video, ids["recap"])
        assert session.get(CatalogTitle, ids["season"]).catalog_collection_id == ids["target"]
        assert session.get(CatalogTitle, ids["extras"]).catalog_collection_id == ids["source"]
        assert recap.file_type == "recap"
        assert recap.content_type_manual == "bonus"
        assert effective_video_content_type(recap) == "bonus"
        assert effective_recap_episode_number(recap) is None


def test_public_collection_move_allows_season_and_recap_dependency_together(
    tmp_path: Path,
):
    web_app, ids, _before = _seed_collection_move_app(
        tmp_path,
        recap_mode="parser",
    )

    response = _public_move_titles(
        web_app,
        ids["target"],
        [ids["season"], ids["extras"]],
    )

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        recap = session.get(Video, ids["recap"])
        assert session.get(CatalogTitle, ids["season"]).catalog_collection_id == ids["target"]
        assert session.get(CatalogTitle, ids["extras"]).catalog_collection_id == ids["target"]
        assert recap.catalog_collection_id == ids["target"]
        assert effective_recap_episode_number(recap) == Decimal("3.5")


def test_public_collection_move_rejects_recap_without_target_season_atomically(
    tmp_path: Path,
):
    web_app, ids, before = _seed_collection_move_app(
        tmp_path,
        recap_mode="parser",
    )

    with pytest.raises(HTTPException) as raised:
        _public_move_titles(web_app, ids["target"], [ids["extras"]])

    assert raised.value.status_code == 400
    assert "Season kontextu" in raised.value.detail
    with web_app.state.sessions() as session:
        assert _all_structural_write_state(session) == before


def test_public_collection_move_allows_unrelated_title(
    tmp_path: Path,
):
    web_app, ids, _before = _seed_collection_move_app(
        tmp_path,
        recap_mode="parser",
        include_unrelated=True,
    )

    response = _public_move_titles(
        web_app,
        ids["target"],
        [ids["unrelated"]],
    )

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        assert session.get(CatalogTitle, ids["unrelated"]).catalog_collection_id == ids["target"]
        assert session.get(CatalogTitle, ids["season"]).catalog_collection_id == ids["source"]
        recap = session.get(Video, ids["recap"])
        assert recap.catalog_collection_id == ids["source"]
        assert effective_recap_episode_number(recap) == Decimal("3.5")


def test_collection_move_recap_validation_query_count_is_bounded_by_collections(
    tmp_path: Path,
):
    def statement_count(label: str, recap_count: int) -> int:
        case_path = tmp_path / label
        case_path.mkdir()
        web_app, ids, _before = _seed_collection_move_app(
            case_path,
            recap_mode="parser",
        )
        with web_app.state.sessions() as session:
            extras = session.get(CatalogTitle, ids["extras"])
            session.get(Video, ids["recap"]).content_type_manual = "bonus"
            numbers = range(10, 10 + recap_count)
            for number in numbers:
                session.add(Video(
                    relative_path=(
                        f"Anime/Show/Season 1 Extras/Recap {number}.5.mkv"
                    ),
                    root_folder="Anime",
                    filename=f"Recap {number}.5.mkv",
                    size=number + 10,
                    mtime_ns=number + 10,
                    file_type="recap",
                    content_type_manual=(
                        None if number == 9 + recap_count else "bonus"
                    ),
                    catalog_collection=extras.collection,
                    catalog_title=extras,
                ))
            session.commit()
            engine = session.get_bind()
        statements = 0

        def count_statement(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", count_statement)
        try:
            with pytest.raises(HTTPException):
                _public_move_titles(web_app, ids["target"], [ids["season"]])
        finally:
            event.remove(engine, "before_cursor_execute", count_statement)
        return statements

    assert statement_count("one", 1) == statement_count("many", 40)


def test_manual_split_rejects_recap_hierarchy_change_before_mutation(
    tmp_path: Path,
):
    web_app, (collection_id, title_id, _recap_id), _before = (
        _seed_single_title_confirmation_app(tmp_path, recap_mode="parser")
    )
    with web_app.state.sessions() as session:
        title = session.get(CatalogTitle, title_id)
        before = _all_structural_write_state(session)
        definition = ManualTitleDefinition(
            title_id=title.id,
            local_title=title.local_title,
            manual_display_title=None,
            season_number_manual=None,
            season_label_manual=None,
            part_number_manual=None,
            part_type_manual="bonus",
            episode_start=None,
            episode_end=None,
            episode_start_offset=None,
            numbering_mode="unknown",
            sort_order=None,
            filename_pattern=None,
            video_ids=(),
        )

        with pytest.raises(ValueError, match="efektivní Recap"):
            apply_manual_split(session, collection_id, [definition])

        assert _all_structural_write_state(session) == before


def test_new_split_season_cannot_make_recap_attachment_ambiguous(
    tmp_path: Path,
):
    web_app, ids, _before = _seed_collection_move_app(
        tmp_path,
        recap_mode="parser",
        include_unrelated=True,
    )
    with web_app.state.sessions() as session:
        season = session.get(CatalogTitle, ids["season"])
        season.part_number_manual = 1
        session.commit()
        unrelated = session.get(CatalogTitle, ids["unrelated"])
        selected_id = unrelated.videos[0].id
        before = _all_structural_write_state(session)

        with pytest.raises(ValueError, match="efektivní Recap"):
            create_title_from_videos(
                session,
                ids["source"],
                [selected_id],
                local_title="Season 1 Part 2",
                part_type="season",
                season_number=1,
                season_label="S1",
                part_number=2,
            )

        assert _all_structural_write_state(session) == before


def test_empty_season_owner_cannot_be_deleted_while_recap_depends_on_it(
    tmp_path: Path,
):
    web_app, ids, _before = _seed_collection_move_app(
        tmp_path,
        recap_mode="parser",
    )
    with web_app.state.sessions() as session:
        season = session.get(CatalogTitle, ids["season"])
        extras = session.get(CatalogTitle, ids["extras"])
        for video in list(season.videos):
            video.catalog_title = extras
        finalize_collection_hierarchy(session.get(CatalogCollection, ids["source"]))
        session.commit()
        assert season.videos == []
        before = _all_structural_write_state(session)

        with pytest.raises(ValueError, match="efektivní Recap"):
            delete_empty_local_title(session, ids["source"], ids["season"])

        assert _all_structural_write_state(session) == before


def test_public_root_video_unassignment_rejects_effective_recap_atomically(
    tmp_path: Path,
):
    web_app, ids, _before = _seed_collection_move_app(
        tmp_path,
        recap_mode="parser",
    )
    with web_app.state.sessions() as session:
        recap = session.get(Video, ids["recap"])
        recap.relative_path = "Recap 3.5.mkv"
        session.commit()
        before = _all_structural_write_state(session)
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/root-videos/{video_id}/assignment"
    )

    with pytest.raises(HTTPException) as raised:
        endpoint(ids["recap"], target_title_id="", confirm_manual=False)

    assert raised.value.status_code == 400
    assert "Season kontextu" in raised.value.detail
    with web_app.state.sessions() as session:
        assert _all_structural_write_state(session) == before


@pytest.mark.parametrize("manual_recap", [True, False], ids=["manual-24.9", "parser-3.5"])
def test_public_title_hierarchy_rejects_mixed_season_to_bonus_atomically(
    tmp_path: Path,
    manual_recap: bool,
):
    web_app, (collection_id, season_id, recap_id), before = (
        _seed_recap_title_hierarchy_app(tmp_path, manual_recap=manual_recap)
    )

    with pytest.raises(HTTPException) as raised:
        _post_title_hierarchy(
            _title_hierarchy_endpoint(web_app),
            collection_id,
            season_id,
            part_type="bonus",
        )

    assert raised.value.status_code == 400
    assert "efektivní Recap" in raised.value.detail
    assert "Season kontext" in raised.value.detail
    with web_app.state.sessions() as session:
        assert _hierarchy_write_state(session, collection_id) == before
        recap = session.get(Video, recap_id)
        assert recap.file_type == "recap"
        assert detect_episode_number(recap.filename).display_value == "3.5"
        if manual_recap:
            assert recap.content_type_manual == "recap"
            assert effective_recap_episode_number(recap) == Decimal("24.9")
        else:
            assert recap.content_type_manual is None
            assert recap.recap_episode_number_manual_tenths is None
            assert effective_recap_episode_number(recap) == Decimal("3.5")


def test_parser_recap_explicit_bonus_allows_followup_title_hierarchy_change(
    tmp_path: Path,
):
    web_app, (collection_id, season_id, recap_id), _before = (
        _seed_recap_title_hierarchy_app(tmp_path, manual_recap=False)
    )
    with web_app.state.sessions() as session:
        recap = session.get(Video, recap_id)
        classify_videos_in_place(session, collection_id, [recap_id], "bonus")
        set_video_episode_number_from_input(recap, "7")
        session.commit()
        assert recap.file_type == "recap"
        assert detect_episode_number(recap.filename).display_value == "3.5"
        assert effective_video_content_type(recap) == "bonus"
        assert effective_recap_episode_number(recap) is None
        assert effective_video_numbering(recap).supplementary_number == 7

    response = _post_title_hierarchy(
        _title_hierarchy_endpoint(web_app),
        collection_id,
        season_id,
        part_type="bonus",
    )

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        season = session.get(CatalogTitle, season_id)
        recap = session.get(Video, recap_id)
        assert season.effective_part_type == "bonus"
        assert recap.catalog_title_id == season_id
        assert recap.content_type_manual == "bonus"
        assert recap.recap_episode_number_manual_tenths is None
        assert recap.file_type == "recap"
        assert detect_episode_number(recap.filename).display_value == "3.5"
        assert effective_recap_episode_number(recap) is None
        assert effective_video_numbering(recap).supplementary_number == 7


def test_manual_recap_clear_then_bonus_allows_followup_title_hierarchy_change(
    tmp_path: Path,
):
    web_app, (collection_id, season_id, recap_id), _before = (
        _seed_recap_title_hierarchy_app(tmp_path, manual_recap=True)
    )
    with web_app.state.sessions() as session:
        recap = session.get(Video, recap_id)
        with pytest.raises(ValueError, match="nebude smazána automaticky"):
            classify_videos_in_place(session, collection_id, [recap_id], "bonus")
        assert recap.content_type_manual == "recap"
        assert recap.recap_episode_number_manual_tenths == 249

        set_video_episode_number_from_input(recap, "")
        classify_videos_in_place(session, collection_id, [recap_id], "bonus")
        set_video_episode_number_from_input(recap, "7")
        session.commit()
        assert recap.file_type == "recap"
        assert recap.content_type_manual == "bonus"
        assert recap.recap_episode_number_manual_tenths is None
        assert effective_recap_episode_number(recap) is None

    response = _post_title_hierarchy(
        _title_hierarchy_endpoint(web_app),
        collection_id,
        season_id,
        part_type="bonus",
    )

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        season = session.get(CatalogTitle, season_id)
        recap = session.get(Video, recap_id)
        assert season.effective_part_type == "bonus"
        assert recap.catalog_title_id == season_id
        assert recap.file_type == "recap"
        assert recap.content_type_manual == "bonus"
        assert effective_video_numbering(recap).supplementary_number == 7


def test_public_title_hierarchy_allows_season_edit_with_effective_recap(
    tmp_path: Path,
):
    web_app, (collection_id, season_id, recap_id), _before = (
        _seed_recap_title_hierarchy_app(tmp_path, manual_recap=True)
    )

    response = _post_title_hierarchy(
        _title_hierarchy_endpoint(web_app),
        collection_id,
        season_id,
        part_type="season",
        season_number="2",
        season_label="S2",
    )

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        season = session.get(CatalogTitle, season_id)
        recap = session.get(Video, recap_id)
        assert (season.effective_part_type, season.effective_season_number) == (
            "season", 2,
        )
        assert recap.catalog_title_id == season_id
        assert recap.content_type_manual == "recap"
        assert effective_recap_episode_number(recap) == Decimal("24.9")


def test_primary_title_edit_protects_recap_in_attached_supplementary_title(
    tmp_path: Path,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'attached-recap-title.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
            hierarchy_status="verified",
            hierarchy_verified_at=utc_now(),
        )
        season = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1",
            part_type="season",
            season_number=1,
            season_label="S1",
        )
        extras = CatalogTitle(
            collection=collection,
            local_title="Season 1 Extras",
            normalized_local_title="season 1 extras",
            relative_root_path="Anime/Show/Season 1 Extras",
            part_type="bonus",
            season_number=1,
            season_label="S1",
        )
        for title, part_type in ((season, "season"), (extras, "bonus")):
            activate_manual_hierarchy_snapshot(
                title,
                part_type=part_type,
                season_number=1,
                part_number=None,
                season_label="S1",
                sort_order=None,
                verified_at=utc_now(),
            )
        recap = Video(
            relative_path="Anime/Show/Season 1 Extras/Recap 3.5.mkv",
            root_folder="Anime",
            filename="Recap 3.5.mkv",
            size=1,
            mtime_ns=1,
            file_type="recap",
            catalog_collection=collection,
            catalog_title=extras,
        )
        session.add(collection)
        session.commit()
        collection_id, season_id, recap_id = collection.id, season.id, recap.id
        before = _hierarchy_write_state(session, collection_id)

    with pytest.raises(HTTPException) as raised:
        _post_title_hierarchy(
            _title_hierarchy_endpoint(web_app),
            collection_id,
            season_id,
            part_type="bonus",
        )

    assert raised.value.status_code == 400
    assert "Season kontext" in raised.value.detail
    with web_app.state.sessions() as session:
        assert _hierarchy_write_state(session, collection_id) == before
        recap = session.get(Video, recap_id)
        assert recap.catalog_title is not None
        assert recap.catalog_title.local_title == "Season 1 Extras"
        assert effective_recap_episode_number(recap) == Decimal("3.5")


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
