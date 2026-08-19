from datetime import datetime, timezone

import pytest

from app.catalog import (
    FILTER_LABELS,
    ROOT_VIDEO_GROUP_LABEL,
    SeriesSummary,
    build_catalog_results,
    catalog_collection_display_title,
    catalog_title_display_title,
    catalog_title_series_label,
    classify_video,
    derive_episode_number,
    detect_episode_number,
    filename_display_title,
    derive_season_info,
    determine_parent_series,
    group_videos_by_series,
    is_film_video,
    manual_hardsub_state,
    normalize_language,
    title_filename_display_title,
    set_manual_hardsub,
    sort_title_videos,
    subtitle_track_display,
    title_videos,
    translation_status,
    unresolved_duplicate_video_ids,
    video_matches_filter,
)
from app.models import (
    CatalogCollection, CatalogTitle, ExternalSubtitle, ExternalTitleLink,
    InternalSubtitle, TitleMetadata, Video,
)


def test_normalizes_czech_variants():
    assert {normalize_language(value, None) for value in ("cs", "cze", "ces")} == {"cs"}


def test_normalizes_slovak_variants():
    assert {normalize_language(value, None) for value in ("sk", "slk", "slo")} == {"sk"}


def test_uses_english_title_when_language_is_unknown():
    assert normalize_language("unknown", "English (UK)") == "eng"
    assert normalize_language("unknown", "[Isekai]") == "unknown"


def test_catalog_title_display_title_prefers_manual_then_metadata_then_local():
    title = CatalogTitle(
        local_title="Local title", normalized_local_title="local title",
        relative_root_path="Anime/Local title", manual_display_title="Manual title",
        metadata_record=TitleMetadata(display_title="Metadata title"),
    )

    assert catalog_title_display_title(title) == "Manual title"
    title.manual_display_title = None
    assert catalog_title_display_title(title) == "Metadata title"
    title.metadata_record = None
    assert catalog_title_display_title(title) == "Local title"


@pytest.mark.parametrize(("preference", "expected"), [
    ("romaji", "Ansatsu Kyoushitsu"),
    ("english", "Assassination Classroom"),
    ("native", "暗殺教室"),
])
def test_catalog_title_display_title_uses_preferred_metadata_variant(
    preference, expected,
):
    title = CatalogTitle(
        local_title="Serie 1", normalized_local_title="serie 1",
        relative_root_path="Anime/Show/Serie 1",
        metadata_record=TitleMetadata(
            display_title="Assassination Classroom",
            title_english="Assassination Classroom",
            title_romaji="Ansatsu Kyoushitsu", title_native="暗殺教室",
        ),
    )

    assert catalog_title_display_title(title, preference) == expected


def test_missing_preferred_metadata_variant_uses_deterministic_metadata_fallback():
    title = CatalogTitle(
        local_title="Serie 1", normalized_local_title="serie 1",
        relative_root_path="Anime/Show/Serie 1",
        metadata_record=TitleMetadata(
            display_title="Romaji only", title_romaji="Romaji only",
        ),
    )

    assert catalog_title_display_title(title, "english") == "Romaji only"
    assert catalog_title_display_title(title, "native") == "Romaji only"


@pytest.mark.parametrize(("preference", "full_title", "collection_title"), [
    ("romaji", "Example Anime - Part 1", "Example Anime"),
    ("english", "Example Anime English - Part 1", "Example Anime English"),
    ("native", "原題 - Part 1", "原題"),
])
def test_collection_strips_structural_part_suffix_confirmed_by_hierarchy(
    preference, full_title, collection_title,
):
    collection = CatalogCollection(
        local_title="Physical Anime (P21)",
        normalized_local_title="physical anime p21",
        relative_root_path="Anime/Physical Anime (P21)",
    )
    title = CatalogTitle(
        collection=collection,
        local_title="Part 1",
        normalized_local_title="part 1",
        relative_root_path="Anime/Physical Anime (P21)/Part 1",
        part_type="part",
        part_number=1,
        sort_order=1,
        metadata_record=TitleMetadata(
            display_title="Example Anime English - Part 1",
            title_romaji="Example Anime - Part 1",
            title_english="Example Anime English - Part 1",
            title_native="原題 - Part 1",
        ),
    )

    assert catalog_collection_display_title(
        collection, preference, titles=[title]
    ) == collection_title
    assert catalog_title_display_title(title, preference) == full_title


def test_collection_uses_common_part_base_confirmed_by_sibling_titles():
    collection = CatalogCollection(
        local_title="Physical Example (L20)",
        normalized_local_title="physical example l20",
        relative_root_path="Anime/Physical Example (L20)",
    )
    titles = [
        CatalogTitle(
            collection=collection,
            local_title=f"Local {number}",
            normalized_local_title=f"local {number}",
            relative_root_path=f"Anime/Physical Example (L20)/Local {number}",
            part_type="title",
            sort_order=number,
            metadata_record=TitleMetadata(
                display_title=f"Example Anime - Part {number}",
                title_romaji=f"Example Anime - Part {number}",
            ),
        )
        for number in (1, 2)
    ]

    assert catalog_collection_display_title(
        collection, "romaji", titles=titles
    ) == "Example Anime"


@pytest.mark.parametrize(("name", "part_type"), [
    ("The Part-Time Hero", "part"),
    ("Example Part 1: The Return", "part"),
    ("Legitimate Title Part 1", "title"),
])
def test_collection_does_not_strip_part_without_structural_suffix_context(
    name, part_type,
):
    collection = CatalogCollection(
        local_title="Physical fallback",
        normalized_local_title="physical fallback",
        relative_root_path="Anime/Physical fallback",
    )
    title = CatalogTitle(
        collection=collection,
        local_title="Local title",
        normalized_local_title="local title",
        relative_root_path="Anime/Physical fallback/Local title",
        part_type=part_type,
        sort_order=1,
        metadata_record=TitleMetadata(display_title=name, title_romaji=name),
    )

    assert catalog_collection_display_title(
        collection, "romaji", titles=[title]
    ) == name


def test_collection_without_display_metadata_keeps_local_fallback():
    collection = CatalogCollection(
        local_title="Local fallback (Z20)",
        normalized_local_title="local fallback z20",
        relative_root_path="Anime/Local fallback (Z20)",
    )
    title = CatalogTitle(
        collection=collection,
        local_title="Part 1",
        normalized_local_title="part 1",
        relative_root_path="Anime/Local fallback (Z20)/Part 1",
        part_type="part",
        sort_order=1,
    )

    assert catalog_collection_display_title(
        collection, "romaji", titles=[title]
    ) == "Local fallback (Z20)"


@pytest.mark.parametrize(("filename", "expected"), [
    ("Ansatsu Kyoushitsu 01.mp4", "Ansatsu Kyoushitsu"),
    ("Ansatsu Kyoushitsu - 01.mkv", "Ansatsu Kyoushitsu"),
    ("Title 00.mp4", "Title"),
    ("Title 14.5.mkv", "Title"),
    ("86 01.mkv", "86"),
])
def test_filename_display_title_removes_only_safe_episode_suffix(filename, expected):
    assert filename_display_title(filename) == expected


def test_numeric_title_without_episode_suffix_is_not_aggressively_stripped():
    assert filename_display_title("86.mkv") is None
    assert filename_display_title("Episode 01.mkv") is None
    title = CatalogTitle(
        local_title="86", normalized_local_title="86", relative_root_path="Anime/86",
    )
    video = Video(
        relative_path="Anime/86/86.mkv", root_folder="Anime", filename="86.mkv",
        size=1, mtime_ns=1, catalog_title=title,
    )
    assert catalog_title_display_title(title, "romaji") == "86"


@pytest.mark.parametrize("preference", ["romaji", "english", "native"])
def test_shared_filename_prefix_wins_over_technical_local_part_name(preference):
    title = CatalogTitle(
        local_title="Serie 1", normalized_local_title="serie 1",
        relative_root_path="Anime/Show/Serie 1",
    )
    title.videos = [Video(
        relative_path=f"Anime/Show/Serie 1/Ansatsu Kyoushitsu {number:02}.mp4",
        root_folder="Anime", filename=f"Ansatsu Kyoushitsu {number:02}.mp4",
        size=1, mtime_ns=1,
    ) for number in range(1, 4)]

    assert title_filename_display_title(title.videos) == "Ansatsu Kyoushitsu"
    assert catalog_title_display_title(title, preference) == "Ansatsu Kyoushitsu"


def test_conflicting_filename_prefixes_use_local_fallback():
    title = CatalogTitle(
        local_title="Serie 1", normalized_local_title="serie 1",
        relative_root_path="Anime/Show/Serie 1",
    )
    title.videos = [
        Video(relative_path="a", root_folder="Anime", filename="First 01.mkv", size=1, mtime_ns=1),
        Video(relative_path="b", root_folder="Anime", filename="Second 02.mkv", size=1, mtime_ns=1),
    ]

    assert title_filename_display_title(title.videos) is None
    assert catalog_title_display_title(title, "romaji") == "Serie 1"


def test_changing_display_preference_is_read_only_for_catalog_and_metadata():
    metadata = TitleMetadata(
        display_title="English", title_english="English", title_romaji="Romaji",
        title_native="Native",
    )
    link = ExternalTitleLink(
        provider="anilist", external_id="1", match_method="manual_search",
        is_primary=True,
    )
    title = CatalogTitle(
        local_title="Serie 1", normalized_local_title="serie 1",
        relative_root_path="Anime/Show/Serie 1", metadata_record=metadata,
        external_links=[link],
    )
    video = Video(
        relative_path="Anime/Show/Serie 1/Show 01.mkv", root_folder="Anime",
        filename="Show 01.mkv", size=1, mtime_ns=1, catalog_title=title,
    )
    before = (
        title.local_title, metadata.display_title, metadata.title_english,
        metadata.title_romaji, metadata.title_native, link.external_id,
        video.filename, video.relative_path,
    )

    assert catalog_title_display_title(title, "english") == "English"
    assert catalog_title_display_title(title, "romaji") == "Romaji"
    assert catalog_title_display_title(title, "native") == "Native"
    assert before == (
        title.local_title, metadata.display_title, metadata.title_english,
        metadata.title_romaji, metadata.title_native, link.external_id,
        video.filename, video.relative_path,
    )


def test_catalog_title_series_label_uses_effective_hierarchy_values():
    title = CatalogTitle(
        local_title="Season", normalized_local_title="season",
        relative_root_path="Anime/Show/Season", part_type="season", season_number=1,
        season_label="S1", season_number_manual=2, season_label_manual="S2",
    )
    assert catalog_title_series_label(title) == "S2"

    title.season_label_manual = None
    title.season_label = None
    assert catalog_title_series_label(title) == "S2"


def test_root_videos_use_workflow_group_instead_of_fake_dot_collection():
    pseudo_collection = CatalogCollection(
        id=1, local_title="Knihovna", normalized_local_title="knihovna",
        relative_root_path=".",
    )
    pseudo_title = CatalogTitle(
        id=1, collection=pseudo_collection, local_title="Knihovna",
        normalized_local_title="knihovna", relative_root_path=".",
    )
    videos = [
        Video(
            id=index, relative_path=filename, root_folder=".", filename=filename,
            size=1, mtime_ns=1, file_type="other", catalog_collection=pseudo_collection,
            catalog_title=pseudo_title,
        )
        for index, filename in enumerate(("Film A.mkv", "Film B.mkv"), 1)
    ]

    results = build_catalog_results(videos, "all")

    assert len(results.groups) == 1
    assert results.groups[0].name == ROOT_VIDEO_GROUP_LABEL
    assert results.groups[0].is_root_group is True
    assert results.groups[0].catalog_collection_id is None
    assert results.groups[0].bonus == 2
    assert results.videos_by_title["."] == videos


def test_root_videos_with_distinct_manual_assignments_stay_in_distinct_collections():
    videos = []
    for index, name in enumerate(("Film A", "Film B"), 1):
        collection = CatalogCollection(
            id=index, local_title=name, normalized_local_title=name.casefold(),
            relative_root_path=f"@root/{index}",
        )
        title = CatalogTitle(
            id=index, collection=collection, local_title=name,
            normalized_local_title=name.casefold(), relative_root_path=f"@root/{index}/title",
        )
        videos.append(Video(
            id=index, relative_path=f"{name}.mkv", root_folder=".", filename=f"{name}.mkv",
            size=1, mtime_ns=1, file_type="other", catalog_collection=collection,
            catalog_title=title,
        ))

    results = build_catalog_results(videos, "all")

    assert [group.name for group in results.groups] == ["Film A", "Film B"]
    assert all(not group.is_root_group for group in results.groups)
    assert [group.catalog_collection_id for group in results.groups] == [1, 2]


def test_root_video_without_catalog_title_keeps_meaningful_collection_assignment():
    collection = CatalogCollection(
        id=3, local_title="Existing collection", normalized_local_title="existing collection",
        relative_root_path="Anime/Existing collection",
    )
    video = Video(
        id=3, relative_path="Unassigned Special.mkv", root_folder=".",
        filename="Unassigned Special.mkv", size=1, mtime_ns=1, file_type="special",
        catalog_collection=collection, catalog_title=None,
    )

    results = build_catalog_results([video], "all")

    assert results.groups[0].name == "Existing collection"
    assert results.groups[0].catalog_collection_id == 3
    assert results.groups[0].is_root_group is False

def test_subtitle_track_display_merges_languages_and_preserves_known_formats():
    video = _video("Anime/Show/01.mkv")
    video.internal_subtitles = [InternalSubtitle(
        stream_index=2, codec="ass", language="cze", normalized_language="cs",
    )]
    video.external_subtitles = [ExternalSubtitle(
        relative_path="Anime/Show/01.en.srt", codec="srt", language="eng",
        normalized_language="eng",
    )]

    tracks = subtitle_track_display(video)

    assert [track.label for track in tracks] == ["CZ (ASS)", "EN (SRT)"]
    assert "interní" in tracks[0].details
    assert "externí" in tracks[1].details
    assert subtitle_track_display(_video("Anime/Show/02.mkv")) == []


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
    groups = group_videos_by_series(videos, "missing")
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
    groups = group_videos_by_series(videos, "all")
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

    groups = group_videos_by_series(videos, "all")

    assert len(groups) == 1
    assert groups[0].name == title
    assert groups[0].relative_path == title
    assert groups[0].total == 2


@pytest.mark.parametrize("directory, expected", [
    ("Serie 1", "S1"), ("Serie 2", "S2"), ("Season 01", "S1"),
    ("S01", "S1"), ("Specials", "Specials"), ("OVA", "OVA"),
])
def test_derives_season_label(directory, expected):
    info = derive_season_info(f"Anime/Show/{directory}/01.mkv")
    assert info.label == expected
    assert info.original == directory


def test_derives_safe_episode_numbers():
    assert derive_episode_number("E01.mkv") == 1
    assert derive_episode_number("EP02.mkv") == 2
    assert derive_episode_number("Episode 10.mkv") == 10
    assert derive_episode_number("01v2.mkv") == 1
    assert derive_episode_number("Show 2024 1080p.mkv") is None


@pytest.mark.parametrize(("filename", "season", "episode", "title"), [
    ("S01E01.mkv", 1, 1, None),
    ("S1E1.mkv", 1, 1, None),
    ("S01E01-Pneumococcus.mkv", 1, 1, "Pneumococcus"),
    ("S1E2 Title.mkv", 1, 2, "Title"),
    ("S02E12 - Title.mkv", 2, 12, "Title"),
    ("S01E01_Title.mkv", 1, 1, "Title"),
    ("S01-E01.Title.mkv", 1, 1, "Title"),
])
def test_sxxexx_keeps_season_episode_and_local_title_candidate(
    filename, season, episode, title,
):
    detection = detect_episode_number(filename)

    assert detection.is_standard
    assert detection.season_hint == season
    assert detection.number == episode
    assert detection.filename_episode_hint == episode
    assert detection.title_candidate == title
    assert derive_episode_number(filename) == episode
    assert classify_video(f"Anime/Show/{filename}") == "episode"


def test_bracketed_sp_overrides_sxxexx_without_inventing_canonical_number():
    filename = "S01E14 [SP]-The Common Cold.mkv"

    detection = detect_episode_number(filename)

    assert detection.is_supplementary
    assert detection.supplementary_type == "special"
    assert detection.supplementary_number is None
    assert detection.season_hint == 1
    assert detection.filename_episode_hint == 14
    assert detection.title_candidate == "The Common Cold"
    assert derive_episode_number(filename) is None
    assert classify_video(f"Anime/Hataraku Saibou/Serie 1/{filename}") == "special"


@pytest.mark.parametrize(("filename", "expected"), [
    ("Title - 01.mkv", 1),
    ("Title - 02.mkv", 2),
    ("Title - 12.mkv", 12),
    ("100-man no Inochi no Ue ni Ore wa Tatteiru - 01.mkv", 1),
])
def test_derives_trailing_hyphen_episode_number(filename, expected):
    assert derive_episode_number(filename) == expected


@pytest.mark.parametrize(("filename", "expected"), [
    ("Title 01.mkv", 1),
    ("Title 02.mp4", 2),
    ("Title 22.mp4", 22),
])
def test_derives_safe_plain_trailing_episode_number(filename, expected):
    assert derive_episode_number(filename) == expected


def test_zero_is_nonstandard_and_not_regular_episode_zero():
    detection = detect_episode_number("Title 00.mp4")

    assert detection.kind == "zero"
    assert detection.display_value == "00"
    assert derive_episode_number("Title 00.mp4") is None
    assert classify_video("Anime/Title 00.mp4") == "other"


def test_fractional_episode_is_detected_without_rounding_to_integer():
    detection = detect_episode_number("Title 14.5.mkv")

    assert detection.kind == "fractional"
    assert detection.number == 14
    assert detection.fraction == "5"
    assert detection.display_value == "14.5"
    assert derive_episode_number("Title 14.5.mkv") is None
    assert classify_video("Anime/Title 14.5.mkv") == "other"


@pytest.mark.parametrize(("filename", "supplementary_type", "number", "display"), [
    ("Title - OVA 01.mkv", "ova", 1, "OVA 01"),
    ("Title - OVA 02.mkv", "ova", 2, "OVA 02"),
    ("Title S2 - OVA P2.mkv", "ova", 2, "OVA 02"),
    ("Title - Special 01.mkv", "special", 1, "Special 01"),
    ("Title - Special 02.mkv", "special", 2, "Special 02"),
    ("OP 01.mkv", "op", 1, "OP 01"),
    ("OP 02.mkv", "op", 2, "OP 02"),
    ("ED 01.mkv", "ed", 1, "ED 01"),
    ("ED 02.mkv", "ed", 2, "ED 02"),
    ("NCOP 01.mkv", "ncop", 1, "NCOP 01"),
    ("NCED 01.mkv", "nced", 1, "NCED 01"),
])
def test_explicit_supplementary_sequence_is_not_standard_episode(
    filename, supplementary_type, number, display,
):
    detection = detect_episode_number(filename)

    assert detection.is_supplementary
    assert detection.supplementary_type == supplementary_type
    assert detection.supplementary_number == number
    assert detection.display_value == display
    assert derive_episode_number(filename) is None


def test_generic_part_suffix_and_season_hint_are_not_episode_numbers():
    assert derive_episode_number("Title P1.mkv") is None
    assert derive_episode_number("Title S2.mkv") is None


@pytest.mark.parametrize("filename", [
    "100-man no Inochi no Ue ni Ore wa Tatteiru.mkv",
    "Anime title (P20-L21).mkv",
    "Anime title 2024.mkv",
    "Anime title 1080p x265 10bit.mkv",
    "Anime title [Release Group 12].mkv",
    "86.mkv",
])
def test_does_not_derive_numbers_inside_title_or_technical_suffixes(filename):
    assert derive_episode_number(filename) is None


def test_title_detail_sorting_orders_seasons_episodes_and_bonus():
    videos = [
        _video("Anime/Show/Serie 2/02.mkv"),
        _video("Anime/Show/Serie 1/10.mkv"),
        _video("Anime/Show/Serie 1/02.mkv"),
        _video("Anime/Show/Serie 1/NCOP.mkv"),
    ]
    videos[-1].file_type = "ncop"
    ordered = title_videos(videos, "Anime/Show")
    assert [video.filename for video in ordered] == ["02.mkv", "10.mkv", "02.mkv", "NCOP.mkv"]
    assert [derive_season_info(video.relative_path).label for video in ordered] == ["S1", "S1", "S2", "S1"]


def test_all_main_catalog_filters_return_grouped_title_summaries():
    videos = [
        _video("Anime/CS/01.mkv", language="cs"),
        _video("Anime/SK/01.mkv", language="sk"),
        _video("Anime/Missing/01.mkv"),
        _video("Anime/Unknown/01.mkv", language="unknown"),
    ]
    both = _video("Anime/Both/01.mkv", language="cs")
    both.external_subtitles = [ExternalSubtitle(
        relative_path="Anime/Both/01.sk.srt", codec="srt", language="sk",
        normalized_language="sk",
    )]
    videos.append(both)

    for filter_name in ("only-cs", "only-sk", "both", "missing", "unknown", "episodes"):
        groups = group_videos_by_series(videos, filter_name)
        assert groups
        assert all(isinstance(group, SeriesSummary) for group in groups)
    assert set(FILTER_LABELS) >= {
        "only-cs", "only-sk", "both", "missing", "unknown", "episodes",
        "films", "bonus",
    }


def test_film_filter_uses_effective_hierarchy_and_excludes_other_parts():
    def assigned_video(
        identifier: int, collection_name: str, part_type: str,
        *, manual_type: str | None = None,
    ) -> Video:
        collection = CatalogCollection(
            id=identifier, local_title=collection_name,
            normalized_local_title=collection_name.casefold(),
            relative_root_path=f"Anime/{collection_name}",
        )
        title = CatalogTitle(
            id=identifier, collection=collection, local_title=part_type,
            normalized_local_title=part_type,
            relative_root_path=f"Anime/{collection_name}/{part_type}",
            part_type=part_type, part_type_manual=manual_type,
            hierarchy_manual_override=manual_type is not None,
        )
        return Video(
            id=identifier,
            relative_path=f"Anime/{collection_name}/{part_type}.mkv",
            root_folder="Anime", filename=f"{part_type}.mkv", size=1,
            mtime_ns=identifier,
            file_type="episode" if part_type == "season" else "other",
            catalog_collection=collection, catalog_title=title,
        )

    automatic_film = assigned_video(1, "Automatic Film", "film")
    manual_film = assigned_video(2, "Manual Film", "title", manual_type="film")
    mixed_episode_title = CatalogTitle(
        id=7, collection=automatic_film.catalog_collection,
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Automatic Film/Season 1", part_type="season",
    )
    mixed_episode = Video(
        id=7, relative_path="Anime/Automatic Film/Season 1/E01.mkv",
        root_folder="Anime", filename="E01.mkv", size=1, mtime_ns=7,
        file_type="episode", catalog_collection=automatic_film.catalog_collection,
        catalog_title=mixed_episode_title,
    )
    episode = assigned_video(3, "TV Series", "season")
    ova = assigned_video(4, "OVA Work", "ova")
    special = assigned_video(5, "Special Work", "special")
    bonus = assigned_video(6, "Bonus Work", "bonus")
    nonfilms = [mixed_episode, episode, ova, special, bonus]
    videos = [automatic_film, manual_film, *nonfilms]

    assert is_film_video(automatic_film) is True
    assert is_film_video(manual_film) is True
    assert all(is_film_video(video) is False for video in nonfilms)
    assert all(video_matches_filter(video, "films") for video in videos[:2])
    assert all(not video_matches_filter(video, "films") for video in nonfilms)

    results = build_catalog_results(videos, "films")
    assert {group.name for group in results.groups} == {
        "Automatic Film", "Manual Film",
    }
    mixed_group = next(
        group for group in results.groups if group.name == "Automatic Film"
    )
    assert mixed_group.total == 2
    assert mixed_group.problematic == 1


def test_search_by_title_is_case_insensitive_and_returns_all_filtered_title_videos():
    videos = [
        _video("Anime/Overlord/Season 01/01.mkv"),
        _video("Anime/Overlord/Season 02/02.mkv"),
    ]
    results = build_catalog_results(videos, "all", "oVeRlOrD")
    assert [group.name for group in results.groups] == ["Overlord"]
    assert results.video_count == 2


def test_search_by_filename_and_relative_path_keeps_parent_title():
    videos = [
        _video("Anime/Overlord/Season 01/Rare Episode E03.mkv"),
        _video("Anime/Overlord/Season 01/ordinary.mkv"),
    ]
    filename_results = build_catalog_results(videos, "all", "rare episode")
    path_results = build_catalog_results(videos, "all", "season 01/rare")
    assert filename_results.groups[0].name == "Overlord"
    assert filename_results.video_count == 1
    assert path_results.video_count == 1


def test_search_by_season_label_and_episode_number_limits_title_detail():
    videos = [
        _video("Anime/Show/Serie 1/E02.mkv"),
        _video("Anime/Show/Serie 2/E10.mkv"),
    ]
    season_one = build_catalog_results(videos, "all", "S1")
    season_two = build_catalog_results(videos, "all", "S2")
    episode = build_catalog_results(videos, "all", "10")
    assert [video.filename for video in season_one.videos_by_title["Anime/Show"]] == ["E02.mkv"]
    assert [video.filename for video in season_two.videos_by_title["Anime/Show"]] == ["E10.mkv"]
    assert [video.filename for video in episode.videos_by_title["Anime/Show"]] == ["E10.mkv"]


def test_search_combines_with_missing_translation_filter():
    missing = _video("Anime/Show/Season 01/Wanted E01.mkv")
    translated = _video("Anime/Show/Season 01/Wanted E02.mkv", language="cs")
    results = build_catalog_results([missing, translated], "missing", "wanted")
    assert results.video_count == 1
    assert results.videos_by_title["Anime/Show"] == [missing]


def test_empty_search_returns_regular_filtered_overview():
    videos = [_video("Anime/One/01.mkv"), _video("Anime/Two/01.mkv")]
    normal = build_catalog_results(videos, "all")
    whitespace = build_catalog_results(videos, "all", "   ")
    assert [group.relative_path for group in whitespace.groups] == [
        group.relative_path for group in normal.groups
    ]
    assert whitespace.video_count == normal.video_count == 2


def test_manual_duplicate_filter_selects_only_explicit_suspicions():
    unreviewed = _video("Anime/Show/E01.mkv")
    suspected = _video("Anime/Show/E02.mkv")
    suspected.duplicate_status_manual = "suspected"

    assert unreviewed.duplicate_status_manual is None
    assert video_matches_filter(unreviewed, "manual-duplicate-suspected") is False
    assert video_matches_filter(suspected, "manual-duplicate-suspected") is True

    results = build_catalog_results(
        [unreviewed, suspected], "manual-duplicate-suspected"
    )
    assert results.video_count == 1
    assert next(iter(results.videos_by_title.values())) == [suspected]
    assert FILTER_LABELS["manual-duplicate-suspected"] == (
        "Ruční podezření na duplicitu"
    )


def _all_duplicates_filter_videos():
    collection = CatalogCollection(
        id=20, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        id=10, collection=collection, catalog_collection_id=collection.id,
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1",
    )

    def video(identifier: int, episode: int, *, suspected: bool = False) -> Video:
        return Video(
            id=identifier,
            relative_path=f"Anime/Show/Season 1/Show {identifier}.mkv",
            root_folder="Anime", filename=f"Show {identifier}.mkv",
            size=1, mtime_ns=identifier, file_type="episode",
            season_episode_number=episode,
            duplicate_status_manual="suspected" if suspected else None,
            catalog_title=title, catalog_title_id=title.id,
            catalog_collection=collection, catalog_collection_id=collection.id,
        )

    videos = {
        "unresolved": video(1, 1),
        "unresolved_suspected": video(2, 1, suspected=True),
        "normal": video(3, 2),
        "manual_only": video(4, 3, suspected=True),
        "primary": video(5, 4),
        "confirmed_suspected": video(6, 4, suspected=True),
    }
    videos["confirmed_suspected"].duplicate_of_video_id = videos["primary"].id
    return videos


def test_all_duplicates_filter_is_current_unresolved_or_confirmed_only():
    videos = _all_duplicates_filter_videos()
    all_videos = list(videos.values())

    unresolved_ids = unresolved_duplicate_video_ids(all_videos)
    results = build_catalog_results(all_videos, "all-duplicates")
    matched_ids = {
        video.id
        for title_videos_list in results.videos_by_title.values()
        for video in title_videos_list
    }

    assert unresolved_ids == {1, 2}
    assert matched_ids == {1, 2, 6}
    assert videos["normal"].id not in matched_ids
    assert videos["manual_only"].id not in matched_ids
    assert videos["primary"].id not in matched_ids
    assert videos["unresolved_suspected"].id in matched_ids
    assert videos["confirmed_suspected"].id in matched_ids
    assert FILTER_LABELS["all-duplicates"] == "Všechny duplicity"


def test_confirmed_copy_remains_filtered_after_unresolved_problem_is_resolved():
    videos = _all_duplicates_filter_videos()
    resolved_group = [videos["primary"], videos["confirmed_suspected"]]
    unresolved_ids = unresolved_duplicate_video_ids(resolved_group)

    assert unresolved_ids == set()
    assert video_matches_filter(
        videos["confirmed_suspected"], "all-duplicates",
        unresolved_duplicate_ids=unresolved_ids,
    ) is True
    assert video_matches_filter(
        videos["primary"], "all-duplicates",
        unresolved_duplicate_ids=unresolved_ids,
    ) is False


def test_manual_duplicate_filter_remains_independent_from_all_duplicates_filter():
    videos = _all_duplicates_filter_videos()
    all_videos = list(videos.values())

    manual_results = build_catalog_results(
        all_videos, "manual-duplicate-suspected"
    )
    manual_ids = {
        video.id
        for title_videos_list in manual_results.videos_by_title.values()
        for video in title_videos_list
    }

    assert manual_ids == {2, 4, 6}


def test_title_sorting_ascending_descending_and_naturally():
    videos = [
        _video("Anime/B/01.mkv"), _video("Anime/A/01.mkv"),
        _video("Anime/Anime 10/01.mkv"), _video("Anime/Anime 2/01.mkv"),
    ]
    ascending = build_catalog_results(videos, "all", sort="title", direction="asc")
    descending = build_catalog_results(videos, "all", sort="title", direction="desc")
    names = [group.name for group in ascending.groups]
    assert names.index("A") < names.index("B")
    assert names.index("Anime 2") < names.index("Anime 10")
    assert [group.name for group in descending.groups] == list(reversed(names))


def test_search_relevance_is_stable_and_prefers_exact_then_title_prefix():
    videos = [
        _video("Anime/Star/01.mkv"),
        _video("Anime/Star Wars/01.mkv"),
        _video("Anime/Lone Star/01.mkv"),
        _video("Anime/Other/Star-file.mkv"),
    ]
    results = build_catalog_results(videos, "all", "Star")
    assert [group.name for group in results.groups] == ["Star", "Star Wars", "Lone Star", "Other"]

    stable = build_catalog_results([
        _video("Anime/Saga 10/01.mkv"), _video("Anime/Saga 2/01.mkv")
    ], "all", "S")
    assert [group.name for group in stable.groups] == ["Saga 2", "Saga 10"]


def test_invalid_sort_and_direction_use_safe_defaults():
    videos = [_video("Anime/A/01.mkv"), _video("Anime/B/01.mkv")]
    results = build_catalog_results(videos, "all", sort="drop table", direction="sideways")
    assert (results.sort, results.direction) == ("matched", "desc")
    bad_direction = build_catalog_results(videos, "all", sort="title", direction="sideways")
    assert (bad_direction.sort, bad_direction.direction) == ("title", "asc")


def test_explicit_video_sorting_uses_natural_filename_order():
    videos = [_video("Anime/Show/E10.mkv"), _video("Anime/Show/E2.mkv")]
    ordered, sort, direction = sort_title_videos(videos, "filename", "asc")
    assert [video.filename for video in ordered] == ["E2.mkv", "E10.mkv"]
    assert (sort, direction) == ("filename", "asc")


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


def test_manual_hardsub_state_distinguishes_unknown_absent_and_present():
    video = _video("Anime/Show/01.mkv")
    verified_at = datetime(2026, 8, 5, 10, 30, tzinfo=timezone.utc)

    assert manual_hardsub_state(video) == "unknown"

    set_manual_hardsub(video, "none", verified_at=verified_at)
    assert manual_hardsub_state(video) == "no"
    assert video.manual_hardsub_verified_at == verified_at

    set_manual_hardsub(video, "cs", verified_at=verified_at)
    assert manual_hardsub_state(video) == "yes"

    set_manual_hardsub(video, "unknown")
    assert manual_hardsub_state(video) == "unknown"
    assert video.manual_hardsub_verified_at is None

def test_clearing_hardsub_verification_falls_back_to_automatic_subtitles():
    video = _video("Anime/Show/01.mkv", language="cs")
    set_manual_hardsub(video, "both")
    set_manual_hardsub(video, "unknown")
    status = translation_status(video)
    assert status.has_cs is True
    assert status.has_sk is False
    assert video.manual_hardsub_verified_at is None


def test_catalog_aggregates_multiple_metadata_titles_as_one_collection():
    collection = CatalogCollection(
        id=10, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    first = CatalogTitle(
        id=11, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", season_number=1,
        season_label="S1", metadata_status="linked_manual", collection=collection,
    )
    second = CatalogTitle(
        id=12, local_title="Season 2", normalized_local_title="season 2",
        relative_root_path="Anime/Show/Season 2", season_number=2,
        season_label="S2", metadata_status="unlinked", collection=collection,
    )
    videos = [
        _video("Anime/Show/Season 1/E01.mkv"),
        _video("Anime/Show/Season 2/E01.mkv"),
    ]
    videos[0].catalog_title, videos[0].catalog_title_id = first, first.id
    videos[1].catalog_title, videos[1].catalog_title_id = second, second.id
    results = build_catalog_results(videos, "all")
    assert len(results.groups) == 1
    assert results.groups[0].catalog_collection_id == collection.id
    assert results.groups[0].parts == 2
    assert results.groups[0].linked_parts == 1
    assert results.groups[0].total == 2
