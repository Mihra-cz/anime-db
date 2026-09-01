import asyncio
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import HTTPException
import pytest
from sqlalchemy import select
from starlette.requests import Request

from app.catalog import set_manual_hardsub
from app.config import Settings
from app.database import Base
from app.main import create_app
from app.media_check import (
    build_media_check_evaluation,
    build_media_check_results,
    set_czsk_availability_manual,
)
from app.models import (
    AudioTrack, CatalogCollection, CatalogTitle, ExternalSubtitle,
    ExternalSubtitleCompatibility, InternalSubtitle,
    UnresolvedExternalSubtitle, Video,
)
from app.subtitle_review import build_unresolved_subtitle_rows


def _video(
    number: int,
    *,
    audio: tuple[str, ...] = ("ja",),
    internal: tuple[str, ...] = (),
    external: tuple[str, ...] = (),
    title: CatalogTitle | None = None,
    collection: CatalogCollection | None = None,
) -> Video:
    external_subtitles = [
        ExternalSubtitle(
            relative_path=f"Anime/Media Show/Season 1/E{number:02}.{index}.ass",
            codec="ass",
            language=language,
            normalized_language=language,
        )
        for index, language in enumerate(external, 1)
    ]
    video = Video(
        id=number,
        relative_path=f"Anime/Media Show/Season 1/E{number:02}.mkv",
        root_folder="Anime",
        filename=f"E{number:02}.mkv",
        size=number,
        mtime_ns=number,
        file_type="episode",
        local_episode_number=number,
        season_episode_number=number,
        catalog_title=title,
        catalog_collection=collection,
        audio_tracks=[
            AudioTrack(stream_index=index, codec="aac", language=language)
            for index, language in enumerate(audio, 1)
        ],
        internal_subtitles=[
            InternalSubtitle(
                stream_index=index,
                codec="ass",
                language=language,
                normalized_language=language,
            )
            for index, language in enumerate(internal, 10)
        ],
    )
    for subtitle in external_subtitles:
        ExternalSubtitleCompatibility(
            external_subtitle=subtitle,
            video=video,
            status="automatic_match",
            match_method="filename",
        )
    return video


def _external_assets(video: Video) -> list[ExternalSubtitle]:
    return [
        row.external_subtitle
        for row in video.external_subtitle_compatibilities
    ]


def _collection() -> tuple[CatalogCollection, CatalogTitle]:
    collection = CatalogCollection(
        id=1,
        local_title="Media Show",
        normalized_local_title="media show",
        relative_root_path="Anime/Media Show",
        hierarchy_status="verified",
        hierarchy_note="stable hierarchy",
    )
    title = CatalogTitle(
        id=1,
        collection=collection,
        local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Media Show/Season 1",
        part_type="season",
        season_number=1,
        season_label="S1",
        sort_order=1,
    )
    return collection, title


@pytest.mark.parametrize(
    ("internal", "external", "manual", "expected"),
    [
        (("cs",), (), None, "available"),
        (("sk",), (), "unavailable", "available"),
        ((), ("cs",), "unavailable", "available"),
        (("en",), (), None, "needs_cs_sk_internal_en"),
        ((), ("en",), None, "needs_cs_sk_no_fallback"),
        (("en",), (), "unavailable", "known_unavailable_internal_en"),
        ((), (), "unavailable", "known_unavailable_no_fallback"),
    ],
)
def test_media_check_workflow_keeps_factual_and_manual_states_separate(
    internal, external, manual, expected,
):
    video = _video(1, internal=internal, external=external)
    set_czsk_availability_manual(video, manual)

    evaluation = build_media_check_evaluation(video)

    assert evaluation.subtitle_status == expected
    assert evaluation.factual.subtitle_status == (
        "preferred"
        if set(internal + external) & {"cs", "sk"}
        else "fallback_internal_en"
        if "en" in internal
        else "missing"
    )


def test_real_czsk_hardsub_overrides_recorded_unavailable_decision():
    video = _video(1)
    set_czsk_availability_manual(video, "unavailable")
    set_manual_hardsub(video, "cs")

    evaluation = build_media_check_evaluation(video)

    assert evaluation.factual.subtitle_status == "preferred"
    assert evaluation.subtitle_status == "available"
    assert evaluation.manual_unavailable_recorded is True
    assert evaluation.manual_unavailable_effective is False


def test_clear_manual_unavailable_returns_to_factual_workflow():
    video = _video(1, internal=("en",))
    set_czsk_availability_manual(video, "unavailable")
    assert build_media_check_evaluation(video).subtitle_status == (
        "known_unavailable_internal_en"
    )

    set_czsk_availability_manual(video, None)

    assert video.czsk_availability_manual is None
    assert build_media_check_evaluation(video).subtitle_status == (
        "needs_cs_sk_internal_en"
    )


def test_media_check_summary_filters_search_and_pagination_share_evaluator():
    collection, title = _collection()
    videos = [
        _video(1, audio=("ja",), internal=("cs",), title=title, collection=collection),
        _video(2, audio=("unknown",), internal=("en",), title=title, collection=collection),
        _video(3, audio=("en",), external=("en",), title=title, collection=collection),
        _video(4, audio=("de",), title=title, collection=collection),
        _video(5, audio=(), internal=("en",), title=title, collection=collection),
        _video(6, audio=("ja",), title=title, collection=collection),
    ]
    set_czsk_availability_manual(videos[4], "unavailable")
    set_czsk_availability_manual(videos[5], "unavailable")

    all_results = build_media_check_results(
        videos, subtitle_filter="all", page_size=20,
    )

    assert all_results.subtitle_counts == {
        "all": 6,
        "unresolved": 3,
        "unresolved-internal-en": 1,
        "unresolved-no-fallback": 2,
        "unavailable": 2,
        "available": 1,
    }
    assert all_results.audio_counts == {
        "all": 6,
        "unknown": 1,
        "english_only": 1,
        "other_known": 1,
        "no_audio": 1,
        "japanese": 2,
    }

    combined = build_media_check_results(
        videos,
        subtitle_filter="unresolved",
        audio_filter="unknown",
        page_size=20,
    )
    assert [row.video.id for row in combined.rows] == [2]
    assert combined.subtitle_counts["unresolved"] == 1
    assert combined.audio_counts["unknown"] == 1
    assert combined.audio_counts["english_only"] == 1
    assert combined.audio_counts["other_known"] == 1

    searched = build_media_check_results(
        videos, subtitle_filter="all", query="E03.mkv", page_size=20,
    )
    assert [row.video.id for row in searched.rows] == [3]
    assert searched.subtitle_counts["unresolved"] == 1

    paged = build_media_check_results(
        videos, subtitle_filter="all", page=2, page_size=2,
    )
    assert paged.total_filtered == 6
    assert paged.total_pages == 3
    assert paged.page == 2
    assert len(paged.rows) == 2

    for filter_name, count in all_results.subtitle_counts.items():
        filtered = build_media_check_results(
            videos, subtitle_filter=filter_name, page_size=20,
        )
        assert filtered.total_filtered == count


def test_confirmed_duplicate_copy_keeps_facts_without_new_completion_unit():
    collection, title = _collection()
    primary = _video(
        1, external=("cs",), title=title, collection=collection,
    )
    copy = _video(2, title=title, collection=collection)
    copy.season_episode_number = 1
    copy.duplicate_of = primary
    copy.duplicate_of_video_id = primary.id

    results = build_media_check_results(
        [primary, copy], subtitle_filter="all", page_size=10,
    )
    assert results.subtitle_counts == {
        "all": 2,
        "unresolved": 0,
        "unresolved-internal-en": 0,
        "unresolved-no-fallback": 0,
        "unavailable": 0,
        "available": 1,
    }
    copy_row = next(row for row in results.rows if row.video is copy)
    assert copy_row.evaluation.completion_required is False
    assert copy_row.evaluation.subtitle_status == "needs_cs_sk_no_fallback"

    shared_asset = _external_assets(primary)[0]
    shared_asset.id = 100
    copy.external_subtitle_compatibilities.append(ExternalSubtitleCompatibility(
        external_subtitle=shared_asset,
        status="confirmed_compatible",
        match_method="manual",
        verified_at=datetime.now(timezone.utc),
    ))
    technical = build_media_check_results(
        [primary, copy], subtitle_filter="all", page_size=10,
    )
    copy_row = next(row for row in technical.rows if row.video is copy)
    assert copy_row.evaluation.subtitle_status == "available"
    assert copy_row.external_subtitle_state.compatible_subtitles == (shared_asset,)
    assert build_media_check_results(
        [primary, copy], subtitle_filter="available", page_size=10,
    ).total_filtered == 1


def _request(web_app, path: str) -> Request:
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


def _post_request(web_app, path: str, items: list[tuple[str, str]]) -> Request:
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


def _media_app(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'media-check.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Partial Translation",
            normalized_local_title="partial translation",
            relative_root_path="Anime/Partial Translation",
            hierarchy_status="verified",
            hierarchy_note="must stay unchanged",
        )
        title = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Partial Translation/Season 1",
            part_type="season",
            season_number=1,
            season_label="S1",
            sort_order=1,
        )
        videos = []
        for number in range(1, 13):
            internal = ("cs",) if number <= 8 else ("en",) if number in {9, 11} else ()
            external = ("en",) if number == 12 else ()
            audio = ("unknown",) if number == 9 else ("ja",)
            video = _video(
                number,
                audio=audio,
                internal=internal,
                external=external,
                title=title,
                collection=collection,
            )
            video.id = None
            video.relative_path = (
                f"Anime/Partial Translation/Season 1/E{number:02}.mkv"
            )
            videos.append(video)
        session.add(collection)
        session.commit()
        ids = {video.season_episode_number: video.id for video in videos}
        audio_track_id = videos[8].audio_tracks[0].id
        external_subtitle_id = _external_assets(videos[11])[0].id
        collection_id = collection.id
    return web_app, ids, audio_track_id, external_subtitle_id, collection_id


def test_partial_translation_bulk_set_clear_is_atomic_and_hierarchy_isolated(tmp_path):
    web_app, ids, _, _, collection_id = _media_app(tmp_path)
    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }
    endpoint = endpoints["/media-check/czsk-availability"]
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        hierarchy_before = (collection.hierarchy_status, collection.hierarchy_note)

    invalid_request = _post_request(web_app, "/media-check/czsk-availability", [
        ("video_ids", str(ids[9])),
        ("video_ids", "999999"),
        ("action", "unavailable"),
    ])
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(endpoint(invalid_request))
    assert exc_info.value.status_code == 404
    with web_app.state.sessions() as session:
        assert session.get(Video, ids[9]).czsk_availability_manual is None

    contradictory_request = _post_request(
        web_app, "/media-check/czsk-availability", [
            ("video_ids", str(ids[1])),
            ("video_ids", str(ids[9])),
            ("action", "unavailable"),
        ],
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(endpoint(contradictory_request))
    assert exc_info.value.status_code == 400
    with web_app.state.sessions() as session:
        assert session.get(Video, ids[1]).czsk_availability_manual is None
        assert session.get(Video, ids[9]).czsk_availability_manual is None

    set_request = _post_request(web_app, "/media-check/czsk-availability", [
        *(("video_ids", str(ids[number])) for number in range(9, 13)),
        ("action", "unavailable"),
        ("return_to", "/media-check?subtitle=unresolved&audio=all"),
    ])
    response = asyncio.run(endpoint(set_request))
    assert response.status_code == 303

    with web_app.state.sessions() as session:
        videos = list(session.scalars(select(Video).order_by(Video.season_episode_number)))
        assert all(video.czsk_availability_manual is None for video in videos[:8])
        assert all(
            video.czsk_availability_manual == "unavailable" for video in videos[8:]
        )
        assert [
            build_media_check_evaluation(video).subtitle_status for video in videos[8:]
        ] == [
            "known_unavailable_internal_en",
            "known_unavailable_no_fallback",
            "known_unavailable_internal_en",
            "known_unavailable_no_fallback",
        ]
        collection = session.get(CatalogCollection, collection_id)
        assert (collection.hierarchy_status, collection.hierarchy_note) == hierarchy_before

    clear_request = _post_request(web_app, "/media-check/czsk-availability", [
        ("video_ids", str(ids[10])),
        ("action", "clear"),
    ])
    asyncio.run(endpoint(clear_request))

    with web_app.state.sessions() as session:
        assert build_media_check_evaluation(
            session.get(Video, ids[10])
        ).subtitle_status == "needs_cs_sk_no_fallback"
        assert session.get(Video, ids[9]).czsk_availability_manual == "unavailable"
        collection = session.get(CatalogCollection, collection_id)
        assert (collection.hierarchy_status, collection.hierarchy_note) == hierarchy_before


def test_media_check_page_navigation_controls_and_existing_review_pages(tmp_path):
    web_app, ids, audio_track_id, external_subtitle_id, _ = _media_app(tmp_path)
    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }

    homepage = endpoints["/"](_request(web_app, "/"), q="")
    hierarchy = endpoints["/hierarchy-review"](
        _request(web_app, "/hierarchy-review"), message=None,
    )
    metadata = endpoints["/metadata-review"](
        _request(web_app, "/metadata-review"), status="without",
    )
    media = endpoints["/media-check"](
        _request(web_app, "/media-check"),
        subtitle="all",
        audio="all",
        q="",
        page=1,
        message=None,
    )
    assert [response.status_code for response in (homepage, hierarchy, metadata, media)] == [
        200, 200, 200, 200,
    ]
    rendered = media.body.decode()
    assert 'href="/media-check"' in homepage.body.decode()
    assert "Doplnit CZ/SK" in rendered
    assert "CZ/SK nyní nejsou dostupné" in rendered
    assert 'action="/media-check/czsk-availability"' in rendered
    assert f'action="/videos/{ids[9]}/audio-tracks/{audio_track_id}/language"' in rendered
    assert (
        f'action="/videos/{ids[12]}/external-subtitles/{external_subtitle_id}/language"'
        in rendered
    )
    assert f'action="/videos/{ids[10]}/hardsub"' in rendered
    assert '/hardsub"' not in hierarchy.body.decode()

    return_to = "/media-check?subtitle=all&audio=all#video-test"
    audio_response = endpoints[
        "/videos/{video_id}/audio-tracks/{track_id}/language"
    ](
        ids[9], audio_track_id, manual_language="ja", filter_name="all",
        series_path="", catalog_title_id=None, q="", sort="", direction="",
        video_sort="", video_direction="", return_to=return_to,
    )
    subtitle_response = endpoints[
        "/videos/{video_id}/external-subtitles/{subtitle_id}/language"
    ](
        ids[12], external_subtitle_id, manual_language="cs", filter_name="all",
        series_path="", catalog_title_id=None, q="", sort="", direction="",
        video_sort="", video_direction="", return_to=return_to,
    )
    hardsub_response = endpoints["/videos/{video_id}/hardsub"](
        ids[10], mode="cs", filter_name="all", series_path="",
        catalog_title_id=None, q="", sort="", direction="", video_sort="",
        video_direction="", return_to=return_to,
    )
    assert {
        audio_response.headers["location"],
        subtitle_response.headers["location"],
        hardsub_response.headers["location"],
    } == {return_to}

    with web_app.state.sessions() as session:
        audio_video = session.get(Video, ids[9])
        subtitle_video = session.get(Video, ids[12])
        hardsub_video = session.get(Video, ids[10])
        assert build_media_check_evaluation(audio_video).factual.audio_status == "japanese"
        assert build_media_check_evaluation(subtitle_video).subtitle_status == "available"
        assert build_media_check_evaluation(hardsub_video).subtitle_status == "available"


def test_unresolved_subtitle_media_check_manual_workflow_is_persistent_and_scoped(tmp_path):
    web_app, ids, _, _, _ = _media_app(tmp_path)
    with web_app.state.sessions() as session:
        unrelated = Video(
            relative_path="Other/Unrelated.mkv", root_folder="Other",
            filename="Unrelated.mkv", size=1, mtime_ns=1,
        )
        unresolved = UnresolvedExternalSubtitle(
            relative_path="Anime/Partial Translation/Season 1/E1.ass",
            filename="E1.ass", extension=".ass", language="cs",
            normalized_language="cs",
        )
        session.add_all([unrelated, unresolved])
        session.commit()
        unresolved_id = unresolved.id
        unrelated_id = unrelated.id

        videos = list(session.scalars(select(Video).order_by(Video.id)))
        rows = build_unresolved_subtitle_rows([unresolved], videos)
        assert rows[0].candidate_count == 1
        assert unrelated_id not in {item.video.id for item in rows[0].candidates}

    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }
    media = endpoints["/media-check"](
        _request(web_app, "/media-check"), subtitle="all", audio="all",
        q="", page=1, message=None,
    )
    rendered = media.body.decode()
    assert "Nepřiřazené externí titulky" in rendered
    assert "E1.ass" in rendered
    assert "1 dostupných kandidátů" in rendered
    unresolved_section = rendered.split("</section>", 1)[0]
    assert "Unrelated.mkv" not in unresolved_section

    reject_request = _post_request(
        web_app,
        f"/media-check/external-subtitles/{unresolved_id}/reject/{ids[1]}",
        [("return_to", "/media-check")],
    )
    asyncio.run(endpoints[
        "/media-check/external-subtitles/{subtitle_id}/reject/{video_id}"
    ](reject_request, unresolved_id, ids[1]))
    with web_app.state.sessions() as session:
        rejected_subtitle = session.get(UnresolvedExternalSubtitle, unresolved_id)
        assert rejected_subtitle.rejected_video_ids_json == f"[{ids[1]}]"
        rows = build_unresolved_subtitle_rows(
            [rejected_subtitle], list(session.scalars(select(Video)).all()),
        )
        assert ids[1] not in {item.video.id for item in rows[0].candidates}

    clear_request = _post_request(
        web_app, f"/media-check/external-subtitles/{unresolved_id}/decision",
        [("action", "clear_rejections")],
    )
    asyncio.run(endpoints[
        "/media-check/external-subtitles/{subtitle_id}/decision"
    ](clear_request, unresolved_id))

    assign_request = _post_request(
        web_app, f"/media-check/external-subtitles/{unresolved_id}/assign",
        [("video_id", str(ids[1])), ("return_to", "/media-check")],
    )
    asyncio.run(endpoints[
        "/media-check/external-subtitles/{subtitle_id}/assign"
    ](assign_request, unresolved_id))
    with web_app.state.sessions() as session:
        linked = session.scalar(select(ExternalSubtitle).where(
            ExternalSubtitle.relative_path.endswith("E1.ass")
        ))
        assert linked.match_method == "manual"
        assert [
            (row.video_id, row.status, row.match_method)
            for row in linked.compatibilities
        ] == [(ids[1], "confirmed_compatible", "manual")]
        linked_id = linked.id
        assert session.get(UnresolvedExternalSubtitle, unresolved_id) is None

    linked_media = endpoints["/media-check"](
        _request(web_app, "/media-check"), subtitle="all", audio="all",
        q="", page=1, message=None,
    ).body.decode()
    assert "Ručně potvrzeno kompatibilní" in linked_media

    reopen_link_request = _post_request(
        web_app, f"/media-check/external-subtitles/{linked_id}/reopen-link", [],
    )
    asyncio.run(endpoints[
        "/media-check/external-subtitles/{subtitle_id}/reopen-link"
    ](reopen_link_request, linked_id))
    with web_app.state.sessions() as session:
        reopened = session.scalar(select(UnresolvedExternalSubtitle).where(
            UnresolvedExternalSubtitle.relative_path.endswith("E1.ass")
        ))
        reopened_id = reopened.id
        assert reopened.status == "unresolved"

    confirm_request = _post_request(
        web_app, f"/media-check/external-subtitles/{reopened_id}/decision",
        [("action", "confirm_no_match")],
    )
    asyncio.run(endpoints[
        "/media-check/external-subtitles/{subtitle_id}/decision"
    ](confirm_request, reopened_id))
    with web_app.state.sessions() as session:
        assert session.get(
            UnresolvedExternalSubtitle, reopened_id
        ).status == "confirmed_no_match"

    reopen_request = _post_request(
        web_app, f"/media-check/external-subtitles/{reopened_id}/decision",
        [("action", "reopen")],
    )
    asyncio.run(endpoints[
        "/media-check/external-subtitles/{subtitle_id}/decision"
    ](reopen_request, reopened_id))
    with web_app.state.sessions() as session:
        assert session.get(UnresolvedExternalSubtitle, reopened_id).status == "unresolved"
