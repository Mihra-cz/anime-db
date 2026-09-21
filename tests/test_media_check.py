import asyncio
from datetime import datetime, timezone
import re
from urllib.parse import urlencode

from fastapi import HTTPException
import pytest
from sqlalchemy import event, select
from starlette.requests import Request

from app.catalog import is_media_completion_video, set_manual_hardsub
from app.config import Settings
from app.database import Base
from app.main import create_app
from app.media_edit_save import MediaEditValidationError, apply_media_edits
from app.media_check import (
    build_media_check_evaluation,
    build_media_check_results,
    set_czsk_availability_manual,
)
from app.models import (
    AudioTrack, CatalogCollection, CatalogTitle, ExternalSubtitle,
    ExternalSubtitleCompatibility, InternalSubtitle,
    TitleMetadata, UnresolvedExternalSubtitle, Video,
)
from app.numbering import (
    DuplicateRelationState,
    collapses_into_duplicate_primary,
    duplicate_relation_state,
    set_video_episode_number_from_input,
)
from app.subtitle_review import build_unresolved_subtitle_rows


def _video(
    number: int,
    *,
    audio: tuple[str, ...] = ("ja",),
    internal: tuple[str, ...] = (),
    external: tuple[str, ...] = (),
    file_type: str = "episode",
    content_type_manual: str | None = None,
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
        file_type=file_type,
        content_type_manual=content_type_manual,
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


def test_seeking_is_manual_workflow_marker_but_factual_czsk_still_wins():
    video = _video(1, internal=("en",))
    set_czsk_availability_manual(video, "seeking")
    evaluation = build_media_check_evaluation(video)
    assert video.czsk_availability_manual == "seeking"
    assert evaluation.manual_seeking_recorded is True
    assert evaluation.subtitle_status == "needs_cs_sk_internal_en"

    video.internal_subtitles.append(InternalSubtitle(
        stream_index=11, codec="ass", language="cs", normalized_language="cs",
    ))
    evaluation = build_media_check_evaluation(video)
    assert evaluation.subtitle_status == "available"
    assert video.czsk_availability_manual == "seeking"


def test_bulk_audio_and_internal_language_require_exactly_one_track_per_video():
    first = _video(1, audio=("unknown",), internal=("unknown",))
    second = _video(2, audio=("en",), internal=("en",))
    first.audio_tracks[0].id, second.audio_tracks[0].id = 11, 12
    first.internal_subtitles[0].id, second.internal_subtitles[0].id = 21, 22

    changes = apply_media_edits(
        [first, second], bulk_audio="ja", bulk_internal_subtitle="cs",
    )

    assert len(changes) == 4
    assert [track.manual_language for video in (first, second) for track in video.audio_tracks] == ["ja", "ja"]
    assert [track.manual_language for video in (first, second) for track in video.internal_subtitles] == ["cs", "cs"]


@pytest.mark.parametrize(
    ("axis", "audio", "internal", "problem_text"),
    [
        ("audio", (), ("en",), "0 audio stop"),
        ("audio", ("ja", "en"), ("en",), "2 audio stopy"),
        ("internal", ("ja",), (), "0 interní subtitle stop"),
        ("internal", ("ja",), ("en", "unknown"), "2 interní subtitle stopy"),
    ],
)
def test_ambiguous_bulk_track_language_rejects_entire_combined_batch(
    axis, audio, internal, problem_text,
):
    safe = _video(1, audio=("unknown",), internal=("unknown",))
    problem = _video(2, audio=audio, internal=internal)

    with pytest.raises(MediaEditValidationError) as exc_info:
        apply_media_edits(
            [safe, problem],
            bulk_audio="ja" if axis == "audio" else "",
            bulk_internal_subtitle="cs" if axis == "internal" else "",
            hardsub="none", availability="seeking",
        )

    error = exc_info.value
    assert problem_text in error.problem_items[0]
    assert "Žádná část" in error.rejected
    assert error.requested_changes[-2:] == (
        "Hardsub → žádný · ověřeno", "CZ/SK dostupnost → Sháním",
    )
    assert safe.manual_hardsub_verified_at is None
    assert safe.czsk_availability_manual is None
    assert all(track.manual_language is None for track in safe.audio_tracks)
    assert all(track.manual_language is None for track in safe.internal_subtitles)


@pytest.mark.parametrize(
    ("file_type", "title_type", "manual_type"),
    [
        ("episode", "season", None),
        ("other", "film", None),
        ("other", "ova", None),
        ("other", "special", None),
        ("episode", "season", "recap"),
        ("other", "preview", None),
    ],
    ids=("episode", "film", "ova", "special", "recap", "preview"),
)
def test_dialogue_content_still_requires_czsk_subtitles(
    file_type, title_type, manual_type,
):
    title = CatalogTitle(
        id=10,
        local_title=title_type,
        normalized_local_title=title_type,
        relative_root_path=f"Anime/Required/{title_type}",
        part_type=title_type,
    )
    video = _video(
        1,
        file_type=file_type,
        content_type_manual=manual_type,
        title=title,
    )

    evaluation = build_media_check_evaluation(video)

    assert evaluation.subtitle_required is True
    assert evaluation.subtitle_status == "needs_cs_sk_no_fallback"
    assert evaluation.subtitle_is_open is True

    set_czsk_availability_manual(video, "unavailable")
    evaluation = build_media_check_evaluation(video)
    assert evaluation.subtitle_status == "known_unavailable_no_fallback"
    assert evaluation.manual_unavailable_effective is True


@pytest.mark.parametrize("file_type", ("op", "ed", "ncop", "nced"))
def test_opening_ending_without_subtitles_is_not_a_media_check_problem(file_type):
    video = _video(1, file_type=file_type)

    evaluation = build_media_check_evaluation(video)

    assert evaluation.factual.subtitle_status == "missing"
    assert evaluation.subtitle_required is False
    assert evaluation.subtitle_status == "not_required"
    assert evaluation.subtitle_severity == "info"
    assert evaluation.subtitle_is_open is False
    assert evaluation.hardsub_review_recommended is False


@pytest.mark.parametrize(
    ("file_type", "container_type"),
    [
        ("ncop", "special"),
        ("nced", "special"),
        ("op", "bonus"),
        ("ed", "bonus"),
        ("op", "season"),
        ("ed", "season"),
    ],
)
def test_exact_opening_ending_subtype_overrides_title_container(
    file_type, container_type,
):
    title = CatalogTitle(
        id=10,
        local_title=container_type,
        normalized_local_title=container_type,
        relative_root_path=f"Anime/Show/{container_type}",
        part_type=container_type,
    )

    evaluation = build_media_check_evaluation(
        _video(1, file_type=file_type, title=title),
    )

    assert evaluation.subtitle_required is False
    assert evaluation.subtitle_status == "not_required"


@pytest.mark.parametrize(
    ("file_type", "manual_type"),
    [
        ("ncop", "special"),
        ("nced", "special"),
        ("ncop", "preview"),
        ("nced", "ova"),
    ],
)
def test_manual_video_authority_overrides_opening_parser_evidence(
    file_type, manual_type,
):
    video = _video(
        1, file_type=file_type, content_type_manual=manual_type,
    )

    evaluation = build_media_check_evaluation(video)

    assert evaluation.subtitle_required is True
    assert evaluation.subtitle_status == "needs_cs_sk_no_fallback"


def test_generic_bonus_or_nc_item_is_not_implicitly_exempt():
    title = CatalogTitle(
        id=10,
        local_title="NC",
        normalized_local_title="nc",
        relative_root_path="Anime/Show/NC",
        part_type="bonus",
    )

    evaluation = build_media_check_evaluation(
        _video(1, file_type="other", title=title),
    )

    assert evaluation.subtitle_required is True
    assert evaluation.subtitle_status == "needs_cs_sk_no_fallback"


@pytest.mark.parametrize(
    ("file_type", "internal", "external", "factual_status"),
    [
        ("op", ("cs",), ("en",), "preferred"),
        ("ed", ("en",), (), "fallback_internal_en"),
    ],
)
def test_opening_ending_keeps_existing_subtitle_facts(
    file_type, internal, external, factual_status,
):
    video = _video(
        1, file_type=file_type, internal=internal, external=external,
    )

    evaluation = build_media_check_evaluation(video)

    assert evaluation.subtitle_status == "not_required"
    assert evaluation.factual.subtitle_status == factual_status
    assert evaluation.factual.internal_subtitle_languages == frozenset(internal)
    assert evaluation.factual.external_subtitle_languages == frozenset(external)


@pytest.mark.parametrize(
    ("audio", "factual_status", "severity", "requires_review"),
    [
        (("unknown",), "unknown", "info", False),
        (("ja",), "japanese", "success", False),
    ],
)
def test_opening_ending_keeps_audio_facts_without_false_language_review(
    audio, factual_status, severity, requires_review,
):
    evaluation = build_media_check_evaluation(
        _video(1, file_type="op", audio=audio),
    )

    assert evaluation.factual.audio_status == factual_status
    assert evaluation.factual.audio_languages == audio
    assert evaluation.audio_severity == severity
    assert evaluation.audio_requires_review is requires_review


@pytest.mark.parametrize("file_type", ("op", "ed", "ncop", "nced"))
def test_opening_ending_under_special_keeps_facts_out_of_unknown_audio_queue(
    file_type,
):
    title = CatalogTitle(
        id=10,
        local_title="Specials",
        normalized_local_title="specials",
        relative_root_path="Anime/Show/Specials",
        part_type="special",
    )
    video = _video(
        1,
        file_type=file_type,
        title=title,
        audio=("unknown",),
        internal=("cs",),
        external=("en",),
    )

    evaluation = build_media_check_evaluation(video)
    unknown_audio = build_media_check_results(
        [video],
        subtitle_filter="all",
        audio_filter="unknown",
        page_size=10,
    )

    assert evaluation.subtitle_status == "not_required"
    assert evaluation.factual.audio_status == "unknown"
    assert evaluation.factual.audio_languages == ("unknown",)
    assert evaluation.factual.internal_subtitle_languages == frozenset({"cs"})
    assert evaluation.factual.external_subtitle_languages == frozenset({"en"})
    assert evaluation.audio_severity == "info"
    assert evaluation.audio_requires_review is False
    assert unknown_audio.total_filtered == 0
    assert unknown_audio.audio_counts["unknown"] == 0


def test_opening_ending_is_excluded_from_subtitle_and_unknown_audio_queues():
    episode = _video(1, audio=("unknown",))
    opening = _video(2, file_type="op", audio=("unknown",))
    ending = _video(3, file_type="ed", audio=("ja",), internal=("cs",))

    all_results = build_media_check_results(
        [episode, opening, ending], subtitle_filter="all", page_size=10,
    )

    assert all_results.subtitle_counts == {
        "all": 3,
        "unresolved": 1,
        "unresolved-internal-en": 0,
        "unresolved-no-fallback": 1,
        "unavailable": 0,
        "available": 0,
    }
    assert all_results.audio_counts["all"] == 3
    assert all_results.audio_counts["unknown"] == 1
    assert build_media_check_results(
        [episode, opening, ending],
        subtitle_filter="unresolved",
        page_size=10,
    ).rows == (all_results.rows[0],)
    assert build_media_check_results(
        [episode, opening, ending],
        subtitle_filter="all",
        audio_filter="unknown",
        page_size=10,
    ).rows == (all_results.rows[0],)


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


def test_media_check_uses_effective_numbering_for_recap_display_search_and_sort():
    collection, title = _collection()
    episode_five = _video(5, title=title, collection=collection)
    episode_six = _video(6, title=title, collection=collection)
    episode_24 = _video(24, title=title, collection=collection)
    episode_25 = _video(25, title=title, collection=collection)

    manual_55 = _video(
        55, file_type="recap", content_type_manual="recap",
        title=title, collection=collection,
    )
    manual_55.filename = "Inserted recap.mkv"
    manual_55.relative_path = "Anime/Media Show/Season 1/Inserted recap.mkv"
    manual_55.local_episode_number = None
    manual_55.season_episode_number = None
    set_video_episode_number_from_input(manual_55, "5.5")

    manual_249 = _video(
        249, file_type="recap", content_type_manual="recap",
        title=title, collection=collection,
    )
    manual_249.filename = "Recap 3.5.mkv"
    manual_249.relative_path = "Anime/Media Show/Season 1/Recap 3.5.mkv"
    manual_249.local_episode_number = None
    manual_249.season_episode_number = None
    set_video_episode_number_from_input(manual_249, "24.9")

    parser_2425 = _video(
        2425, file_type="recap", title=title, collection=collection,
    )
    parser_2425.filename = "Recap 24.25.mkv"
    parser_2425.relative_path = "Anime/Media Show/Season 1/Recap 24.25.mkv"
    parser_2425.local_episode_number = None
    parser_2425.season_episode_number = None

    manual_bonus = _video(
        350, file_type="recap", content_type_manual="bonus",
        title=title, collection=collection,
    )
    manual_bonus.filename = "Recap 3.5 bonus source.mkv"
    manual_bonus.relative_path = (
        "Anime/Media Show/Season 1/Recap 3.5 bonus source.mkv"
    )
    manual_bonus.local_episode_number = None
    manual_bonus.season_episode_number = None
    manual_bonus.episode_number_manual_override = 7

    videos = [
        episode_25, manual_249, episode_six, parser_2425,
        manual_bonus, episode_five, manual_55, episode_24,
    ]
    results = build_media_check_results(
        videos, subtitle_filter="all", page_size=20,
    )
    rows = {row.video.id: row for row in results.rows}

    assert rows[manual_55.id].episode_label == "E5.5"
    assert rows[manual_249.id].episode_label == "E24.9"
    assert rows[parser_2425.id].episode_label == "E24.25"
    assert rows[manual_bonus.id].episode_label == "Bonus 07"
    assert "3.5" not in rows[manual_bonus.id].episode_label
    assert [row.video.id for row in results.rows] == [
        episode_five.id,
        manual_55.id,
        episode_six.id,
        manual_bonus.id,
        episode_24.id,
        parser_2425.id,
        manual_249.id,
        episode_25.id,
    ]

    for query, expected_id in (
        ("5.5", manual_55.id),
        ("24.9", manual_249.id),
        ("24.25", parser_2425.id),
        ("Bonus 07", manual_bonus.id),
    ):
        searched = build_media_check_results(
            videos,
            subtitle_filter="all",
            query=query,
            page_size=20,
        )
        assert [row.video.id for row in searched.rows] == [expected_id]

    integer_search = build_media_check_results(
        videos,
        subtitle_filter="all",
        query="E24.mkv",
        page_size=20,
    )
    assert [row.video.id for row in integer_search.rows] == [episode_24.id]


def test_confirmed_duplicate_copy_keeps_facts_without_new_completion_unit():
    collection, title = _collection()
    primary = _video(
        1, external=("cs",), title=title, collection=collection,
    )
    copy = _video(2, title=title, collection=collection)
    copy.season_episode_number = 1
    copy.duplicate_of = primary
    copy.duplicate_of_video_id = primary.id

    assert duplicate_relation_state(copy) == DuplicateRelationState.VALID
    assert is_media_completion_video(copy) is False

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


def test_r7_stale_duplicate_secondary_is_media_check_completion_unit():
    collection, title = _collection()
    primary = _video(
        1, external=("cs",), title=title, collection=collection,
    )
    secondary = _video(2, title=title, collection=collection)
    secondary.duplicate_of = primary
    secondary.duplicate_of_video_id = primary.id

    assert duplicate_relation_state(secondary) == DuplicateRelationState.INVALID
    assert collapses_into_duplicate_primary(secondary) is False
    assert is_media_completion_video(secondary) is True
    results = build_media_check_results(
        [primary, secondary], subtitle_filter="unresolved", page_size=10,
    )
    assert results.total_filtered == 1
    assert results.rows[0].video is secondary
    assert results.rows[0].evaluation.completion_required is True


def test_r7_unknown_duplicate_secondary_remains_completion_unit():
    collection, title = _collection()
    primary = _video(
        1, external=("cs",), title=title, collection=collection,
    )
    secondary = _video(2, title=title, collection=collection)
    secondary.filename = "Unknown copy.mkv"
    secondary.relative_path = "Anime/Media Show/Season 1/Unknown copy.mkv"
    secondary.local_episode_number = None
    secondary.season_episode_number = None
    secondary.duplicate_of = primary
    secondary.duplicate_of_video_id = primary.id

    assert duplicate_relation_state(secondary) == DuplicateRelationState.UNKNOWN
    assert collapses_into_duplicate_primary(secondary) is False
    assert is_media_completion_video(secondary) is True

    results = build_media_check_results(
        [primary, secondary], subtitle_filter="unresolved", page_size=10,
    )
    assert results.total_filtered == 1
    assert results.rows[0].video is secondary
    assert results.rows[0].evaluation.completion_required is True


@pytest.mark.parametrize("missing_mode", ("marker", "dangling_fk"))
def test_r7_missing_primary_secondary_remains_completion_unit(missing_mode):
    collection, title = _collection()
    secondary = _video(1, title=title, collection=collection)
    if missing_mode == "marker":
        secondary.duplicate_primary_missing = True
    else:
        secondary.duplicate_of_video_id = 999

    assert duplicate_relation_state(secondary) == DuplicateRelationState.INVALID
    assert collapses_into_duplicate_primary(secondary) is False
    assert is_media_completion_video(secondary) is True

    results = build_media_check_results(
        [secondary], subtitle_filter="unresolved", page_size=10,
    )
    assert results.total_filtered == 1
    assert results.rows[0].video is secondary


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


def test_media_check_get_handles_detached_sibling_title_without_n_plus_one(
    tmp_path,
):
    web_app, _ids, _audio_id, _subtitle_id, collection_id = _media_app(tmp_path)
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/media-check"
    )

    def get_with_query_count():
        statements = 0

        def increment(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", increment)
        try:
            response = endpoint(
                _request(web_app, "/media-check"),
                subtitle="all",
                audio="all",
                q="",
                page=1,
                message=None,
            )
        finally:
            event.remove(engine, "before_cursor_execute", increment)
        return response, statements

    baseline_response, baseline_statements = get_with_query_count()
    assert baseline_response.status_code == 200
    assert baseline_statements <= 8

    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        collection.titles.append(CatalogTitle(
            local_title="Season 2",
            normalized_local_title="season 2",
            relative_root_path="Anime/Partial Translation/Season 2",
            part_type="season",
            season_number=2,
            season_label="S2",
            sort_order=2,
            metadata_record=TitleMetadata(
                display_title="Detached Sibling",
                title_romaji="Detached Sibling",
            ),
        ))
        session.commit()

    response, statements = get_with_query_count()
    assert response.status_code == 200
    assert "Detached Sibling" in response.body.decode()
    assert statements == baseline_statements


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


def test_unified_video_edit_requires_preview_confirmation_and_commits_once(tmp_path):
    web_app, ids, audio_track_id, _, _ = _media_app(tmp_path)
    with web_app.state.sessions() as session:
        video = session.get(Video, ids[9])
        title_id = video.catalog_title_id
        internal_track_id = video.internal_subtitles[0].id
    path = f"/media-check/titles/{title_id}/videos/{ids[9]}/edit"
    endpoint = next(
        route.endpoint for route in web_app.routes
        if route.path == (
            "/media-check/titles/{catalog_title_id}/videos/{video_id}/edit"
        )
    )
    values = [
        (f"audio_{audio_track_id}", "ja"),
        (f"internal_subtitle_{internal_track_id}", "cs"),
        ("hardsub", "none"), ("availability", "seeking"),
        ("return_to", f"/media-check/titles/{title_id}#video-{ids[9]}"),
    ]
    preview = asyncio.run(endpoint(
        _post_request(web_app, path, values), title_id, ids[9],
    ))
    assert preview.status_code == 200
    assert "Opravdu chcete uložit" in preview.body.decode()
    assert "Audio stream" in preview.body.decode()
    assert "Hardsub:" in preview.body.decode()
    assert "CZ/SK dostupnost:" in preview.body.decode()
    with web_app.state.sessions() as session:
        video = session.get(Video, ids[9])
        assert video.czsk_availability_manual is None
        assert video.manual_hardsub_verified_at is None
        assert session.get(AudioTrack, audio_track_id).manual_language is None
        assert video.internal_subtitles[0].normalized_language == "en"
    saved = asyncio.run(endpoint(_post_request(
        web_app, path, values + [("confirm_changes", "yes")],
    ), title_id, ids[9]))
    assert saved.status_code == 303
    with web_app.state.sessions() as session:
        video = session.get(Video, ids[9])
        assert video.czsk_availability_manual == "seeking"
        assert video.manual_hardsub_verified_at is not None
        assert session.get(AudioTrack, audio_track_id).manual_language == "ja"
        assert video.internal_subtitles[0].normalized_language == "en"
        assert video.internal_subtitles[0].manual_language == "cs"


def test_unified_video_validation_preserves_track_input_and_rolls_back(tmp_path):
    web_app, ids, audio_track_id, _, _ = _media_app(tmp_path)
    with web_app.state.sessions() as session:
        video = session.get(Video, ids[9])
        title_id = video.catalog_title_id
        internal_track_id = video.internal_subtitles[0].id
    path = f"/media-check/titles/{title_id}/videos/{ids[9]}/edit"
    endpoint = next(
        route.endpoint for route in web_app.routes
        if route.path == (
            "/media-check/titles/{catalog_title_id}/videos/{video_id}/edit"
        )
    )
    response = asyncio.run(endpoint(_post_request(web_app, path, [
        (f"audio_{audio_track_id}", "ja"),
        (f"internal_subtitle_{internal_track_id}", "xx"),
        ("hardsub", "none"),
        ("availability", "seeking"),
    ]), title_id, ids[9]))

    assert response.status_code == 400
    rendered = response.body.decode()
    assert "Jazyk interní subtitle stopy Stream" in rendered
    assert "Hodnota „xx“ není podporovaný jazyk" in rendered
    assert '<option value="xx" selected>' in rendered
    assert 'data-dirty-on-load="true"' in rendered
    with web_app.state.sessions() as session:
        video = session.get(Video, ids[9])
        assert session.get(AudioTrack, audio_track_id).manual_language is None
        assert video.internal_subtitles[0].manual_language is None
        assert video.manual_hardsub_verified_at is None
        assert video.czsk_availability_manual is None


def test_bulk_media_edit_rolls_back_all_videos_when_one_fails(tmp_path):
    web_app, ids, _, _, _ = _media_app(tmp_path)
    with web_app.state.sessions() as session:
        title_id = session.get(Video, ids[9]).catalog_title_id
        ambiguous = session.get(Video, ids[1])
        ambiguous.audio_tracks.append(AudioTrack(
            stream_index=99, codec="aac", language="en",
        ))
        session.commit()
    endpoint = next(
        route.endpoint for route in web_app.routes
        if route.path == "/media-check/titles/{catalog_title_id}/bulk-edit"
    )
    path = f"/media-check/titles/{title_id}/bulk-edit"
    invalid = [
        ("video_ids", str(ids[9])), ("video_ids", str(ids[1])),
        ("bulk_audio", "en"), ("hardsub", ""),
        ("availability", "unavailable"),
        ("confirm_changes", "yes"),
    ]
    rejected = asyncio.run(endpoint(_post_request(web_app, path, invalid), title_id))
    assert rejected.status_code == 400
    rendered_error = rejected.body.decode()
    assert "Žádná část hromadné změny nebyla uložena" in rendered_error
    assert "právě jednu odpovídající stopu" in rendered_error
    assert "E01.mkv" in rendered_error
    assert "Odeberte problematická videa" in rendered_error
    with web_app.state.sessions() as session:
        assert session.get(Video, ids[9]).audio_tracks[0].manual_language is None
        assert session.get(Video, ids[1]).audio_tracks[0].manual_language is None
        assert session.get(Video, ids[9]).czsk_availability_manual is None

    valid = [
        ("video_ids", str(ids[9])), ("video_ids", str(ids[10])),
        ("bulk_audio", "en"), ("hardsub", ""),
        ("availability", ""),
    ]
    preview = asyncio.run(endpoint(_post_request(web_app, path, valid), title_id))
    assert preview.status_code == 200
    assert "Vybráno 2 videí" in preview.body.decode()
    assert "Audio stream" in preview.body.decode()
    with web_app.state.sessions() as session:
        assert session.get(Video, ids[9]).audio_tracks[0].manual_language is None
    saved = asyncio.run(endpoint(_post_request(
        web_app, path, valid + [("confirm_changes", "yes")],
    ), title_id))
    assert saved.status_code == 303
    with web_app.state.sessions() as session:
        assert session.get(Video, ids[9]).audio_tracks[0].manual_language == "en"
        assert session.get(Video, ids[10]).audio_tracks[0].manual_language == "en"
        assert session.get(Video, ids[9]).czsk_availability_manual is None


def _global_bulk_scope(*, query: str = "") -> list[tuple[str, str]]:
    return [
        ("scope_subtitle", "all"),
        ("scope_audio", "all"),
        ("scope_q", query),
        ("scope_page", "1"),
        ("return_to", "/media-check?subtitle=all&audio=all&page=1"),
    ]


def test_global_bulk_previews_and_atomically_saves_videos_across_titles(tmp_path):
    web_app, ids, _, _, collection_id = _media_app(tmp_path)
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        second_title = CatalogTitle(
            collection=collection,
            local_title="Season 2", normalized_local_title="season 2",
            relative_root_path="Anime/Partial Translation/Season 2",
            part_type="season", season_number=2, season_label="S2",
        )
        session.add(second_title)
        session.flush()
        session.get(Video, ids[10]).catalog_title = second_title
        session.commit()
        second_title_id = second_title.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if route.path == "/media-check/bulk-edit"
    )
    values = [
        ("video_ids", str(ids[9])), ("video_ids", str(ids[10])),
        ("hardsub", "none"), ("availability", "seeking"),
        ("bulk_audio", ""), ("bulk_internal_subtitle", ""),
        *_global_bulk_scope(),
    ]
    preview = asyncio.run(endpoint(_post_request(
        web_app, "/media-check/bulk-edit", values,
    )))
    assert preview.status_code == 200
    rendered = preview.body.decode()
    assert "Vybráno 2 videí" in rendered
    assert "E09.mkv" in rendered and "E10.mkv" in rendered
    with web_app.state.sessions() as session:
        assert session.get(Video, ids[9]).manual_hardsub_verified_at is None
        assert session.get(Video, ids[10]).czsk_availability_manual is None
        assert session.get(Video, ids[9]).catalog_title_id != second_title_id

    saved = asyncio.run(endpoint(_post_request(
        web_app, "/media-check/bulk-edit",
        values + [("confirm_changes", "yes")],
    )))
    assert saved.status_code == 303
    with web_app.state.sessions() as session:
        for video_id in (ids[9], ids[10]):
            video = session.get(Video, video_id)
            assert video.manual_hardsub_verified_at is not None
            assert video.czsk_availability_manual == "seeking"


def test_global_bulk_rejects_selection_outside_rendered_page_without_writes(tmp_path):
    web_app, ids, _, _, _ = _media_app(tmp_path)
    endpoint = next(
        route.endpoint for route in web_app.routes
        if route.path == "/media-check/bulk-edit"
    )
    values = [
        ("video_ids", str(ids[10])), ("hardsub", "none"),
        ("bulk_audio", ""), ("bulk_internal_subtitle", ""),
        ("availability", ""), *_global_bulk_scope(query="E09.mkv"),
    ]

    response = asyncio.run(endpoint(_post_request(
        web_app, "/media-check/bulk-edit", values,
    )))

    assert response.status_code == 400
    rendered = response.body.decode()
    assert "Výběr videí byl odmítnut" in rendered
    assert "právě zobrazené stránce" in rendered
    assert f"Video ID {ids[10]}" in rendered
    assert "Zaškrtněte pouze videa viditelná" in rendered
    with web_app.state.sessions() as session:
        assert session.get(Video, ids[10]).manual_hardsub_verified_at is None


@pytest.mark.parametrize(
    ("extra_values", "expected_reason"),
    [
        ([('hardsub', 'none')], "Vyberte alespoň jedno existující video"),
        ([('video_ids', '9')], "žádnou změněnou hodnotu"),
    ],
    ids=("no-selection", "no-requested-change"),
)
def test_global_bulk_explains_empty_request_without_writes(
    tmp_path, extra_values, expected_reason,
):
    web_app, ids, _, _, _ = _media_app(tmp_path)
    values = [
        ("bulk_audio", ""), ("bulk_internal_subtitle", ""),
        *([] if any(key == "hardsub" for key, _ in extra_values) else [
            ("hardsub", ""),
        ]),
        ("availability", ""),
        *_global_bulk_scope(),
    ]
    if extra_values == [("video_ids", "9")]:
        extra_values = [("video_ids", str(ids[9]))]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if route.path == "/media-check/bulk-edit"
    )

    response = asyncio.run(endpoint(_post_request(
        web_app, "/media-check/bulk-edit", extra_values + values,
    )))

    assert response.status_code == 400
    rendered = response.body.decode()
    assert "Změny" in rendered or "Výběr videí" in rendered
    assert expected_reason in rendered
    assert "Jak pokračovat" in rendered
    with web_app.state.sessions() as session:
        assert session.get(Video, ids[9]).manual_hardsub_verified_at is None


def test_title_bulk_rejects_foreign_video_and_rolls_back_entire_request(tmp_path):
    web_app, ids, _, _, collection_id = _media_app(tmp_path)
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        source_title_id = session.get(Video, ids[9]).catalog_title_id
        foreign_title = CatalogTitle(
            collection=collection,
            local_title="Movie", normalized_local_title="movie",
            relative_root_path="Anime/Partial Translation/Movie",
            part_type="film",
        )
        session.add(foreign_title)
        session.flush()
        session.get(Video, ids[10]).catalog_title = foreign_title
        session.commit()
    endpoint = next(
        route.endpoint for route in web_app.routes
        if route.path == "/media-check/titles/{catalog_title_id}/bulk-edit"
    )
    values = [
        ("video_ids", str(ids[9])), ("video_ids", str(ids[10])),
        ("hardsub", "none"), ("bulk_audio", ""),
        ("bulk_internal_subtitle", ""), ("availability", ""),
    ]

    response = asyncio.run(endpoint(_post_request(
        web_app, f"/media-check/titles/{source_title_id}/bulk-edit", values,
    ), source_title_id))

    assert response.status_code == 400
    rendered = response.body.decode()
    assert "nepatří do této části" in rendered
    assert "E10.mkv" in rendered
    assert "Obnovte stránku" in rendered
    with web_app.state.sessions() as session:
        assert session.get(Video, ids[9]).manual_hardsub_verified_at is None
        assert session.get(Video, ids[10]).manual_hardsub_verified_at is None


def test_unrequested_bulk_language_axis_does_not_apply_track_guard():
    multi_audio = _video(1, audio=("ja", "en"), internal=())

    changes = apply_media_edits(
        [multi_audio], bulk_audio="", bulk_internal_subtitle="",
        hardsub="none",
    )

    assert changes == [
        "E01 · E01.mkv · Hardsub: neověřeno → žádný · ověřeno",
    ]
    assert all(track.manual_language is None for track in multi_audio.audio_tracks)



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
    with web_app.state.sessions() as session:
        title_id = session.get(Video, ids[9]).catalog_title_id
    title_media = endpoints["/media-check/titles/{catalog_title_id}"](
        _request(web_app, f"/media-check/titles/{title_id}"), title_id,
        message=None,
    )
    title_rendered = title_media.body.decode()
    assert 'href="/media-check"' in homepage.body.decode()
    assert "Doplnit CZ/SK" in rendered
    assert "CZ/SK nejsou dostupné" in rendered
    assert rendered.count('data-media-bulk-form') == 1
    assert rendered.count('action="/media-check/bulk-edit"') == 1
    assert rendered.count('data-media-select') == 12
    assert f'name="video_ids" value="{ids[9]}"' in rendered
    assert f'href="/titles/{title_id}"' in rendered
    assert (
        f'href="/media-check/titles/{title_id}?focus_video={ids[9]}'
        f'#video-{ids[9]}">Upravit video</a>'
    ) in rendered
    assert "Otevřít Media Edit" not in rendered
    assert "Upravit média" not in rendered
    assert title_rendered.count(
        f'action="/media-check/titles/{title_id}/bulk-edit"'
    ) == 1
    assert title_rendered.count('data-media-bulk-form') == 1
    assert (
        f'action="/media-check/titles/{title_id}/videos/{ids[9]}/edit"'
        in title_rendered
    )
    assert f'name="audio_{audio_track_id}"' in title_rendered
    assert 'name="internal_subtitle_' in title_rendered
    assert (
        f'action="/videos/{ids[12]}/external-subtitles/{external_subtitle_id}/language"'
        in title_rendered
    )
    assert (
        f'action="/media-check/titles/{title_id}/videos/{ids[10]}/edit"'
        in title_rendered
    )
    assert '/hardsub"' not in hierarchy.body.decode()
    assert "JA – Japonština" in title_rendered
    assert "? – Neznámý jazyk" in title_rendered
    assert "JP audio</span><small>JA – Japonština</small>" in rendered
    assert "Jazyk audia neurčen</span><small>? – Neznámý jazyk</small>" in rendered
    for value, label in (
        ("ja", "JA – Japonština"),
        ("kor", "KO – Korejština"),
        ("zho", "ZH – Čínština"),
        ("deu", "DE – Němčina"),
        ("unknown", "? – Neznámý jazyk"),
    ):
        assert re.search(
            rf'<option value="{value}"(?: selected)?\s*>{re.escape(label)}</option>',
            title_rendered,
        )

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


def test_opening_media_check_ui_is_neutral_and_rejects_unavailable_marker(tmp_path):
    web_app, ids, _, _, _ = _media_app(tmp_path)
    with web_app.state.sessions() as session:
        translated_opening = session.get(Video, ids[1])
        unknown_audio_opening = session.get(Video, ids[9])
        translated_opening.file_type = "op"
        unknown_audio_opening.file_type = "op"
        session.commit()

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    response = endpoints["/media-check"](
        _request(web_app, "/media-check"),
        subtitle="all",
        audio="all",
        q="",
        page=1,
        message=None,
    )
    rendered = response.body.decode()
    translated_row = rendered.split(
        f'id="video-{ids[1]}"', 1,
    )[1].split("</tr>", 1)[0]
    unknown_audio_row = rendered.split(
        f'id="video-{ids[9]}"', 1,
    )[1].split("</tr>", 1)[0]

    assert "Titulky nejsou požadované" in translated_row
    assert "Fakticky: CZ/SK" in translated_row
    assert "JP audio" in translated_row
    assert "Stream 10" not in translated_row
    assert "Titulky nejsou požadované" in unknown_audio_row
    assert "Jazyk audia neurčen" in unknown_audio_row
    assert "severity-info" in unknown_audio_row
    assert "Vhodné ověřit" not in unknown_audio_row
    assert f'name="video_ids" value="{ids[9]}"' in unknown_audio_row
    assert "Upravit video" in unknown_audio_row
    assert "CZ/SK nyní nejsou dostupné</button>" not in unknown_audio_row

    with web_app.state.sessions() as session:
        title_id = session.get(Video, ids[1]).catalog_title_id
    title_rendered = endpoints["/media-check/titles/{catalog_title_id}"](
        _request(web_app, f"/media-check/titles/{title_id}"), title_id,
        message=None,
    ).body.decode()
    assert "Stream 10" in title_rendered
    assert 'data-media-select' in title_rendered

    request = _post_request(web_app, "/media-check/czsk-availability", [
        ("video_ids", str(ids[9])),
        ("action", "unavailable"),
    ])
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(endpoints["/media-check/czsk-availability"](request))
    assert exc_info.value.status_code == 400
    with web_app.state.sessions() as session:
        assert session.get(Video, ids[9]).czsk_availability_manual is None


def test_title_media_focus_opens_only_requested_video_without_selecting_it(tmp_path):
    web_app, ids, _, _, _ = _media_app(tmp_path)
    with web_app.state.sessions() as session:
        title_id = session.get(Video, ids[9]).catalog_title_id
    endpoint = next(
        route.endpoint for route in web_app.routes
        if route.path == "/media-check/titles/{catalog_title_id}"
    )

    default = endpoint(
        _request(web_app, f"/media-check/titles/{title_id}"),
        title_id, message=None, focus_video=None,
    ).body.decode()
    assert '<details class="media-controls" open' not in default

    focused = endpoint(
        _request(
            web_app,
            f"/media-check/titles/{title_id}?focus_video={ids[9]}#video-{ids[9]}",
        ),
        title_id, message=None, focus_video=ids[9],
    ).body.decode()
    assert focused.count('<details class="media-controls" open') == 1
    assert (
        f'class="panel media-title-video is-focus-target" id="video-{ids[9]}"'
        in focused
    )
    target = focused.split(f'id="video-{ids[9]}"', 1)[1].split(
        '</article>', 1,
    )[0]
    target_checkbox = re.search(
        rf'<input[^>]+name="video_ids" value="{ids[9]}"[^>]*>', target,
    ).group(0)
    assert "checked" not in target_checkbox


def test_media_check_selection_is_not_persisted_into_a_new_get(tmp_path):
    web_app, ids, _, _, _ = _media_app(tmp_path)
    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    rejected = asyncio.run(endpoints["/media-check/bulk-edit"](_post_request(
        web_app, "/media-check/bulk-edit", [
            ("video_ids", str(ids[9])),
            ("bulk_audio", ""), ("bulk_internal_subtitle", ""),
            ("hardsub", ""), ("availability", ""),
            *_global_bulk_scope(),
        ],
    )))
    assert rejected.status_code == 400
    rejected_checkbox = re.search(
        rf'<input[^>]+name="video_ids" value="{ids[9]}"[^>]*>',
        rejected.body.decode(),
    ).group(0)
    assert "checked" in rejected_checkbox

    fresh = endpoints["/media-check"](
        _request(web_app, "/media-check"),
        subtitle="all", audio="all", q="", page=1, message=None,
    ).body.decode()
    fresh_checkboxes = re.findall(
        r'<input[^>]+name="video_ids"[^>]*data-media-select[^>]*>', fresh,
    )
    assert len(fresh_checkboxes) == 12
    assert all("checked" not in checkbox for checkbox in fresh_checkboxes)

    filtered = endpoints["/media-check"](
        _request(web_app, "/media-check?q=E09.mkv"),
        subtitle="all", audio="all", q="E09.mkv", page=1, message=None,
    ).body.decode()
    filtered_checkboxes = re.findall(
        r'<input[^>]+name="video_ids"[^>]*data-media-select[^>]*>', filtered,
    )
    assert len(filtered_checkboxes) == 1
    assert "checked" not in filtered_checkboxes[0]


def test_global_media_check_get_is_semantically_read_only(tmp_path):
    web_app, _, _, _, _ = _media_app(tmp_path)
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if route.path == "/media-check"
    )
    writes = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = endpoint(
            _request(web_app, "/media-check"),
            subtitle="all", audio="all", q="", page=1, message=None,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    assert writes == []


def test_focused_title_media_get_is_semantically_read_only(tmp_path):
    web_app, ids, _, _, _ = _media_app(tmp_path)
    with web_app.state.sessions() as session:
        title_id = session.get(Video, ids[9]).catalog_title_id
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if route.path == "/media-check/titles/{catalog_title_id}"
    )
    writes = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = endpoint(
            _request(
                web_app,
                f"/media-check/titles/{title_id}?focus_video={ids[9]}",
            ),
            title_id, message=None, focus_video=ids[9],
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    assert writes == []


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

    with web_app.state.sessions() as session:
        title_id = session.get(Video, ids[1]).catalog_title_id
    linked_media = endpoints["/media-check/titles/{catalog_title_id}"](
        _request(web_app, f"/media-check/titles/{title_id}"),
        title_id, message=None,
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
