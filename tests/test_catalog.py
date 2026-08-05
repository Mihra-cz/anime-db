from datetime import datetime, timezone

import pytest

from app.catalog import (
    classify_video,
    determine_parent_series,
    group_videos_by_series,
    normalize_language,
    set_manual_hardsub,
    translation_status,
)
from app.models import ExternalSubtitle, InternalSubtitle, Video


def test_normalizes_czech_variants():
    assert {normalize_language(value, None) for value in ("cs", "cze", "ces")} == {"cs"}


def test_normalizes_slovak_variants():
    assert {normalize_language(value, None) for value in ("sk", "slk", "slo")} == {"sk"}


def test_uses_english_title_when_language_is_unknown():
    assert normalize_language("unknown", "English (UK)") == "eng"
    assert normalize_language("unknown", "[Isekai]") == "unknown"


def test_internal_czech_marks_video_as_translated():
    video = Video(
        relative_path="Show/01.mkv", root_folder="Show", filename="01.mkv",
        size=1, mtime_ns=1, file_type="episode",
        internal_subtitles=[
            InternalSubtitle(stream_index=2, codec="subrip", language="cze", normalized_language="cs")
        ],
    )
    status = translation_status(video)
    assert status.has_cs
    assert status.has_cs_or_sk
    assert status.subtitle_source == "internal"


def test_video_can_have_czech_and_slovak_at_the_same_time():
    video = Video(
        relative_path="Show/02.mkv", root_folder="Show", filename="02.mkv",
        size=1, mtime_ns=1, file_type="episode",
        internal_subtitles=[
            InternalSubtitle(stream_index=2, codec="ass", language="cze", normalized_language="cs")
        ],
        external_subtitles=[
            ExternalSubtitle(
                relative_path="Show/02.sk.srt", codec="srt", language="sk",
                normalized_language="sk",
            )
        ],
    )

    status = translation_status(video)

    assert status.has_cs is True
    assert status.has_sk is True
    assert status.has_cs_or_sk is True
    assert status.subtitle_source == "both"


def test_classifies_bonus_video_types():
    assert classify_video("Show/NCOP 01.mkv") == "ncop"
    assert classify_video("Show/NCED 02.mkv") == "nced"
    assert classify_video("Show/OVA/Show OVA 1.mkv") == "ova"
    assert classify_video("Show/Specials/Special 01.mkv") == "special"


def test_parent_series_for_video_directly_in_series_directory():
    series = determine_parent_series("Anime/Naruto/episode01.mkv")
    assert (series.name, series.relative_path) == ("Naruto", "Anime/Naruto")


def test_parent_series_ignores_season_directory():
    series = determine_parent_series("Anime/Naruto/Season 01/episode01.mkv")
    assert (series.name, series.relative_path) == ("Naruto", "Anime/Naruto")


def test_parent_series_ignores_specials_directory():
    series = determine_parent_series("Anime/Naruto/Specials/ova01.mkv")
    assert (series.name, series.relative_path) == ("Naruto", "Anime/Naruto")


def test_parent_series_in_deeper_structure():
    series = determine_parent_series("Anime/Collection/Naruto/Season 02/episode01.mkv")
    assert (series.name, series.relative_path) == ("Naruto", "Anime/Collection/Naruto")


@pytest.mark.parametrize(
    "technical_directory",
    ["Serie 1", "Série 2", "Season 01", "S01", "Cour 2", "Part 1", "Specials", "OVA"],
)
def test_parent_series_ignores_all_supported_technical_directories(technical_directory):
    series = determine_parent_series(f"Anime/Naruto/{technical_directory}/episode01.mkv")
    assert (series.name, series.relative_path) == ("Naruto", "Anime/Naruto")


def _video(path: str, *, language: str | None = None) -> Video:
    subtitles = [] if language is None else [
        InternalSubtitle(stream_index=1, codec="ass", language=language, normalized_language=language)
    ]
    return Video(
        relative_path=path, root_folder="Anime", filename=path.rsplit("/", 1)[-1],
        size=1, mtime_ns=1, file_type="episode", internal_subtitles=subtitles,
    )


def test_groups_episodes_and_counts_missing_videos():
    videos = [
        _video("Anime/Naruto/Season 01/01.mkv"),
        _video("Anime/Naruto/Season 01/02.mkv"),
        _video("Anime/Naruto/Season 01/03.mkv", language="cs"),
    ]
    groups = group_videos_by_series(videos, lambda video: not translation_status(video).has_cs_or_sk)
    assert len(groups) == 1
    assert groups[0].name == "Naruto"
    assert groups[0].total == 3
    assert groups[0].problematic == 2
    assert groups[0].translated == 1


def test_multiple_seasons_are_merged_into_one_series_row():
    videos = [
        _video("Anime/Naruto/Serie 1/01.mkv"),
        _video("Anime/Naruto/Série 2/01.mkv"),
        _video("Anime/Naruto/Season 03/01.mkv"),
    ]
    groups = group_videos_by_series(videos, lambda _: True)
    assert len(groups) == 1
    assert groups[0].name == "Naruto"
    assert groups[0].relative_path == "Anime/Naruto"
    assert groups[0].total == 3


@pytest.mark.parametrize(
    ("title", "first_season", "second_season"),
    [
        ("Ansatsu Kyoushitsu (Z15-Z16)", "Serie 1 (Z15)", "Serie 2 (Z16)"),
        ("Darker than Black (J07-P09)", "Serie 1 (J07)", "Serie 2 (P09)"),
    ],
)
def test_season_directories_with_technical_suffix_merge_under_real_title(
    title, first_season, second_season
):
    videos = [
        _video(f"{title}/{first_season}/01.mkv"),
        _video(f"{title}/{second_season}/01.mkv"),
    ]

    groups = group_videos_by_series(videos, lambda _: True)

    assert len(groups) == 1
    assert groups[0].name == title
    assert groups[0].relative_path == title
    assert groups[0].total == 2


def test_manual_hardsub_sets_independent_language_flags_and_timestamp():
    video = _video("Anime/Show/01.mkv")
    verified_at = datetime(2026, 8, 5, 10, 30, tzinfo=timezone.utc)

    set_manual_hardsub(video, "cs", verified_at=verified_at)
    assert translation_status(video).has_cs is True
    assert translation_status(video).has_sk is False
    assert video.manual_hardsub_verified_at == verified_at

    set_manual_hardsub(video, "sk", verified_at=verified_at)
    assert translation_status(video).has_cs is False
    assert translation_status(video).has_sk is True

    set_manual_hardsub(video, "both", verified_at=verified_at)
    assert translation_status(video).has_cs is True
    assert translation_status(video).has_sk is True


def test_clearing_manual_hardsub_falls_back_to_automatic_subtitles():
    video = _video("Anime/Show/01.mkv", language="cs")
    set_manual_hardsub(video, "both")
    set_manual_hardsub(video, "none")
    status = translation_status(video)
    assert status.has_cs is True
    assert status.has_sk is False
    assert video.manual_hardsub_verified_at is None
