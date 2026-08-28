import pytest

from app.catalog import (
    FILE_TYPE_TO_SUPPLEMENTARY_SUBTYPE, classify_video, detect_episode_number,
    sort_title_videos, video_sort_key,
)
from app.models import CatalogCollection, CatalogTitle, TitleMetadata, Video
from app.numbering import (
    apply_sequential_numbering, collection_requires_numbering_review,
    confirmed_duplicate_groups,
    effective_video_numbering,
    preview_sequential_numbering, recalculate_collection_numbering,
    recalculate_title_numbering,
    set_duplicate_group_primary, set_title_numbering, set_video_episode_override,
    summarize_title_numbering, unresolved_duplicate_groups,
)


def videos(start, end):
    return [Video(
        id=number, relative_path=f"Anime/Show/Part 2/Episode {number:02}.mkv",
        root_folder="Anime", filename=f"Episode {number:02}.mkv", size=1, mtime_ns=1,
    ) for number in range(start, end + 1)]


def part(number=2, offset=None, mode="unknown", external=False):
    title = CatalogTitle(
        id=10, local_title=f"Part {number}", normalized_local_title=f"part {number}",
        relative_root_path=f"Anime/Show/Part {number}", part_type="part",
        part_number=number, season_number=1, episode_start_offset=offset,
        numbering_mode=mode,
    )
    if external:
        title.metadata_record = TitleMetadata(
            catalog_title_id=10, display_title="Part", episode_count=13,
        )
    return title


def test_part_one_local_numbers_stay_season_numbers():
    title, items = part(1), videos(1, 13)
    recalculate_title_numbering(title, items)
    assert [item.season_episode_number for item in items] == list(range(1, 14))
    assert [item.absolute_episode_number for item in items] == list(range(1, 14))


def test_part_two_absolute_local_numbers_use_offset_for_season():
    title, items = part(offset=13), videos(14, 26)
    recalculate_title_numbering(title, items)
    assert [item.local_episode_number for item in items] == list(range(14, 27))
    assert [item.season_episode_number for item in items] == list(range(1, 14))
    assert [item.absolute_episode_number for item in items] == list(range(14, 27))


def test_part_two_season_local_numbers_use_offset_for_absolute():
    title, items = part(offset=13), videos(1, 13)
    recalculate_title_numbering(title, items)
    assert [item.season_episode_number for item in items] == list(range(1, 14))
    assert [item.absolute_episode_number for item in items] == list(range(14, 27))


def test_part_two_without_known_offset_does_not_guess_absolute_numbers():
    title, items = part(), videos(1, 13)
    recalculate_title_numbering(title, items)
    assert all(item.absolute_episode_number is None for item in items)


def test_season_one_part_two_keeps_season_scope_and_part_ordinal_separate():
    title, items = part(2), videos(1, 1)

    recalculate_title_numbering(title, items)

    assert title.effective_season_number == 1
    assert title.effective_part_number == 2
    assert items[0].season_episode_number == 1
    assert items[0].absolute_episode_number is None


def test_external_part_two_numbers_follow_season_numbers():
    title, items = part(offset=13, external=True), videos(14, 26)
    recalculate_title_numbering(title, items)
    assert [item.external_episode_number for item in items] == list(range(1, 14))


def test_manual_override_has_priority_and_survives_recalculation():
    title, items = part(offset=13), videos(14, 14)
    set_video_episode_override(items[0], 20)
    recalculate_title_numbering(title, items)
    recalculate_title_numbering(title, items)
    assert items[0].episode_number_manual_override == 20
    assert items[0].episode_number_source == "manual"
    assert items[0].season_episode_number == 7


def test_manual_numbering_settings_are_marked_verified():
    title = part()
    set_title_numbering(title, "absolute", 13)
    assert title.numbering_manual is True
    assert title.numbering_verified_at is not None


def test_manual_episode_number_does_not_change_title_or_season():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        id=10, collection=collection, local_title="Season 1",
        normalized_local_title="season 1", relative_root_path="Anime/Show/Season 1",
        season_number=1, season_label="S1", numbering_mode="season_local",
        episode_start_offset=0,
    )
    video = Video(
        id=1, relative_path="Anime/Show/Season 1/Title - 02.mkv",
        root_folder="Anime", filename="Title - 02.mkv", size=1, mtime_ns=1,
        catalog_title=title, catalog_collection=collection,
    )

    set_video_episode_override(video, 2)
    recalculate_title_numbering(title, [video])

    assert video.catalog_title is title
    assert title.season_number == 1
    assert title.effective_season_number == 1
    assert video.season_episode_number == 2


def test_sequential_preview_is_pure_and_uses_deterministic_filename_order():
    items = list(reversed([
        Video(
            id=number,
            relative_path=f"Anime/Show/Season 1/Title - {number:02}.mkv",
            root_folder="Anime", filename=f"Title - {number:02}.mkv",
            size=1, mtime_ns=1,
        )
        for number in range(1, 13)
    ]))

    rows = preview_sequential_numbering(items, 1)

    assert [(row.filename, row.proposed_episode) for row in rows] == [
        (f"Title - {number:02}.mkv", number) for number in range(1, 13)
    ]
    assert all(item.episode_number_manual_override is None for item in items)
    assert all(item.season_episode_number is None for item in items)


def test_confirmed_sequential_numbering_creates_e1_to_e12_without_changing_season():
    title = part(1, offset=0, mode="season_local")
    title.season_number = 1
    items = list(reversed(videos(1, 12)))
    for item in items:
        item.catalog_title = title

    apply_sequential_numbering(items, 1)
    recalculate_title_numbering(title, items)

    ordered = sorted(items, key=lambda item: item.season_episode_number)
    assert [item.season_episode_number for item in ordered] == list(range(1, 13))
    assert [item.episode_number_manual_override for item in ordered] == list(range(1, 13))
    assert title.season_number == 1
    assert all(item.catalog_title is title for item in items)


def test_sequential_numbering_does_not_silently_overwrite_manual_value():
    items = videos(1, 2)
    set_video_episode_override(items[0], 9)

    with pytest.raises(ValueError, match="explicitně potvrdit"):
        apply_sequential_numbering(items, 1)

    assert items[0].episode_number_manual_override == 9
    assert items[1].episode_number_manual_override is None

    apply_sequential_numbering(items, 1, confirm_manual_conflicts=True)
    assert [item.episode_number_manual_override for item in items] == [1, 2]


def test_offset_is_count_of_preceding_episodes_for_season_local_mode():
    title = part(2, offset=12, mode="season_local")
    items = videos(1, 12)

    recalculate_title_numbering(title, items)

    assert items[0].season_episode_number == 1
    assert items[0].absolute_episode_number == 13
    assert items[-1].season_episode_number == 12
    assert items[-1].absolute_episode_number == 24


def test_unknown_preceding_title_count_prevents_later_inferred_absolute_offset():
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show", relative_root_path="Anime/Show",
    )
    titles = [
        CatalogTitle(
            id=number, collection=collection, local_title=f"Season {number}",
            normalized_local_title=f"season {number}",
            relative_root_path=f"Anime/Show/Season {number}",
            season_number=number, sort_order=number,
        )
        for number in range(1, 4)
    ]
    titles[1].metadata_record = TitleMetadata(
        catalog_title_id=titles[1].id, display_title="Season 2", episode_count=12,
    )
    items = {
        title.id: [Video(
            id=title.id, relative_path=f"{title.relative_root_path}/Episode 01.mkv",
            root_folder="Anime", filename="Episode 01.mkv", size=1, mtime_ns=1,
        )]
        for title in titles
    }

    recalculate_collection_numbering(collection, items)

    assert items[titles[2].id][0].season_episode_number == 1
    assert items[titles[2].id][0].absolute_episode_number is None


def _collection_part(
    collection, *, identifier, season, part_number, count, part_type="part",
):
    label = f"S{season}" if season is not None else f"Part {part_number}"
    title = CatalogTitle(
        id=identifier,
        collection=collection,
        local_title=label,
        normalized_local_title=label.casefold(),
        relative_root_path=f"Anime/Show/{label}-{identifier}",
        part_type=part_type,
        season_number=season,
        part_number=part_number,
        sort_order=(season or 0) * 1000 + (part_number or 0),
        numbering_mode="season_local",
    )
    title.metadata_record = TitleMetadata(
        catalog_title_id=identifier,
        display_title=label,
        episode_count=count,
    )
    title_videos = [
        Video(
            id=identifier * 100 + number,
            relative_path=f"{title.relative_root_path}/E{number:02}.mkv",
            root_folder="Anime",
            filename=f"E{number:02}.mkv",
            size=1,
            mtime_ns=number,
            catalog_title=title,
            catalog_collection=collection,
        )
        for number in range(1, count + 1)
    ]
    return title, title_videos


def test_collection_numbering_orders_s1p1_s1p2_then_s2p1_and_accumulates_offsets():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    s2p1, s2p1_videos = _collection_part(
        collection, identifier=3, season=2, part_number=1, count=12,
    )
    s1p2, s1p2_videos = _collection_part(
        collection, identifier=2, season=1, part_number=2, count=6,
    )
    s1p1, s1p1_videos = _collection_part(
        collection, identifier=1, season=1, part_number=1, count=6,
    )

    recalculate_collection_numbering(collection, {
        s2p1.id: s2p1_videos,
        s1p2.id: s1p2_videos,
        s1p1.id: s1p1_videos,
    })

    assert [video.absolute_episode_number for video in s1p1_videos] == list(range(1, 7))
    assert [video.absolute_episode_number for video in s1p2_videos] == list(range(7, 13))
    assert [video.absolute_episode_number for video in s2p1_videos] == list(range(13, 25))
    assert [video.season_episode_number for video in s2p1_videos] == list(range(1, 13))


def test_collection_numbering_supports_seasons_with_and_without_parts():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    s3, s3_videos = _collection_part(
        collection, identifier=4, season=3, part_number=None, count=2,
        part_type="season",
    )
    s2p2, s2p2_videos = _collection_part(
        collection, identifier=3, season=2, part_number=2, count=3,
    )
    s1, s1_videos = _collection_part(
        collection, identifier=1, season=1, part_number=None, count=6,
        part_type="season",
    )
    s2p1, s2p1_videos = _collection_part(
        collection, identifier=2, season=2, part_number=1, count=3,
    )

    recalculate_collection_numbering(collection, {
        title.id: items for title, items in (
            (s3, s3_videos), (s2p2, s2p2_videos),
            (s1, s1_videos), (s2p1, s2p1_videos),
        )
    })

    assert s1_videos[0].absolute_episode_number == 1
    assert s2p1_videos[0].absolute_episode_number == 7
    assert s2p2_videos[0].absolute_episode_number == 10
    assert s3_videos[0].absolute_episode_number == 13
    assert s1.part_number is None and s3.part_number is None


def test_manual_season_parts_do_not_change_canonical_episode_numbers():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    first = CatalogTitle(
        id=1, collection=collection, local_title="First half",
        normalized_local_title="first half",
        relative_root_path="Anime/Show/.catalog-part-1",
        part_type="season", season_number=1, season_label="S1",
    )
    second = CatalogTitle(
        id=2, collection=collection, local_title="Second half",
        normalized_local_title="second half",
        relative_root_path="Anime/Show/.catalog-part-2",
        part_type="season", season_number=1, season_label="S1",
    )
    first_videos = videos(1, 13)
    second_videos = videos(14, 26)

    recalculate_collection_numbering(collection, {
        first.id: first_videos,
        second.id: second_videos,
    })
    before = (
        [video.season_episode_number for video in first_videos],
        [video.season_episode_number for video in second_videos],
    )

    for title, part_number in ((first, 1), (second, 2)):
        title.part_type_manual = "season"
        title.season_number_manual = 1
        title.season_label_manual = "S1"
        title.part_number_manual = part_number
        title.hierarchy_manual_override = True
    recalculate_collection_numbering(collection, {
        first.id: first_videos,
        second.id: second_videos,
    })

    assert before == (
        list(range(1, 14)),
        list(range(14, 27)),
    )
    assert [video.season_episode_number for video in first_videos] == before[0]
    assert [video.season_episode_number for video in second_videos] == before[1]
    assert (first.effective_season_number, second.effective_season_number) == (1, 1)
    assert (first.effective_part_number, second.effective_part_number) == (1, 2)


def test_standalone_parts_keep_null_season_and_use_part_order_for_offsets():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    part_two, part_two_videos = _collection_part(
        collection, identifier=2, season=None, part_number=2, count=2,
    )
    part_one, part_one_videos = _collection_part(
        collection, identifier=1, season=None, part_number=1, count=2,
    )

    recalculate_collection_numbering(collection, {
        part_two.id: part_two_videos,
        part_one.id: part_one_videos,
    })

    assert part_one.season_number is None and part_two.season_number is None
    assert part_one_videos[0].absolute_episode_number == 1
    assert part_two_videos[0].absolute_episode_number == 3


def test_isolated_s2p1_does_not_masquerade_as_first_structural_title():
    title, items = part(1), videos(1, 2)
    title.season_number = 2

    recalculate_title_numbering(title, items)

    assert [video.season_episode_number for video in items] == [1, 2]
    assert all(video.absolute_episode_number is None for video in items)


def test_supplementary_title_does_not_break_or_increase_canonical_offset():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    s1, s1_videos = _collection_part(
        collection, identifier=1, season=1, part_number=None, count=2,
        part_type="season",
    )
    ova = CatalogTitle(
        id=2, collection=collection, local_title="OVA", normalized_local_title="ova",
        relative_root_path="Anime/Show/OVA", part_type="ova", sort_order=1500,
    )
    ova.metadata_record = TitleMetadata(
        catalog_title_id=ova.id,
        display_title="OVA",
        episode_count=5,
    )
    ova_video = Video(
        id=200, relative_path="Anime/Show/OVA/OVA 01.mkv", root_folder="Anime",
        filename="OVA 01.mkv", size=1, mtime_ns=1,
        catalog_title=ova, catalog_collection=collection,
    )
    s2, s2_videos = _collection_part(
        collection, identifier=3, season=2, part_number=None, count=2,
        part_type="season",
    )

    recalculate_collection_numbering(collection, {
        s1.id: s1_videos, ova.id: [ova_video], s2.id: s2_videos,
    })

    assert ova_video.absolute_episode_number is None
    assert s2_videos[0].absolute_episode_number == 3


def test_media_part_number_does_not_affect_hierarchy_part_offsets():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    first, first_videos = _collection_part(
        collection, identifier=1, season=1, part_number=1, count=2,
    )
    second, second_videos = _collection_part(
        collection, identifier=2, season=1, part_number=2, count=2,
    )
    first_videos[0].media_part_number = 9
    second_videos[0].media_part_number = 1

    recalculate_collection_numbering(collection, {
        first.id: first_videos, second.id: second_videos,
    })

    assert first_videos[0].absolute_episode_number == 1
    assert second_videos[0].absolute_episode_number == 3
    assert (first.effective_part_number, second.effective_part_number) == (1, 2)


def test_verified_collection_with_unknown_numbering_requires_review():
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show", hierarchy_status="verified",
    )
    title = CatalogTitle(
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", collection=collection,
    )
    title.videos = videos(1, 2)

    assert collection_requires_numbering_review(collection) is True
    assert summarize_title_numbering(title.videos).unknown == 2


def test_verified_collection_with_fully_numbered_title_has_no_numbering_warning():
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show", hierarchy_status="verified",
    )
    title = CatalogTitle(
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", collection=collection,
    )
    title.videos = videos(1, 2)
    for number, item in enumerate(title.videos, 1):
        item.season_episode_number = number

    summary = summarize_title_numbering(title.videos)
    assert collection_requires_numbering_review(collection) is False
    assert summary.requires_review is False
    assert summary.gaps == ()


def test_zero_plus_episodes_one_to_twenty_two_has_standard_range_one_to_twenty_two():
    title = CatalogTitle(
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type_manual="season",
    )
    items = [Video(
        id=number + 1,
        relative_path=f"Anime/Show/Season 1/Title {number:02}.mp4",
        root_folder="Anime", filename=f"Title {number:02}.mp4", size=1, mtime_ns=1,
    ) for number in range(23)]

    recalculate_title_numbering(title, items)
    summary = summarize_title_numbering(items, title)

    assert items[0].local_episode_number is None
    assert items[0].season_episode_number is None
    assert items[0].episode_number_source == "nonstandard_zero"
    assert summary.total == 23
    assert summary.standard_total == 22
    assert summary.numbered == 22
    assert summary.nonstandard == 1
    assert (summary.episode_min, summary.episode_max) == (1, 22)
    assert summary.gaps == ()
    assert summary.requires_review is True


def test_manual_e01_override_makes_raw_zero_an_effective_standard_episode():
    title = CatalogTitle(
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/High School DxD Hero/Season 1",
        part_type_manual="season", season_number_manual=1,
    )
    items = videos(2, 13)
    zero = Video(
        id=50,
        relative_path=(
            "Anime/High School DxD Hero/Season 1/High School DxD Hero - 00.mkv"
        ),
        root_folder="Anime", filename="High School DxD Hero - 00.mkv",
        size=1, mtime_ns=1,
    )
    items.append(zero)

    recalculate_title_numbering(title, items)
    assert effective_video_numbering(zero, title).is_nonstandard
    assert zero.episode_number_source == "nonstandard_zero"

    set_video_episode_override(zero, 1)
    recalculate_title_numbering(title, items)
    state = effective_video_numbering(zero, title)
    summary = summarize_title_numbering(items, title)

    assert state.is_standard
    assert state.season_episode_number == 1
    assert state.detection.kind == "zero"
    assert detect_episode_number(zero.filename).kind == "zero"
    assert zero.local_episode_number is None
    assert zero.episode_number_source == "manual"
    assert zero.filename == "High School DxD Hero - 00.mkv"
    assert summary.standard_total == summary.numbered == 13
    assert summary.unknown == summary.nonstandard == 0
    assert (summary.episode_min, summary.episode_max) == (1, 13)
    assert summary.gaps == ()
    assert summary.requires_review is False


def test_manual_override_other_than_one_resolves_raw_zero_by_same_priority():
    title = CatalogTitle(
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type_manual="season",
    )
    zero = Video(
        id=1, relative_path="Anime/Show/Season 1/Show 00.mkv",
        root_folder="Anime", filename="Show 00.mkv", size=1, mtime_ns=1,
    )

    set_video_episode_override(zero, 7)
    recalculate_title_numbering(title, [zero])

    state = effective_video_numbering(zero, title)
    assert state.is_standard
    assert state.season_episode_number == 7
    assert state.detection.kind == "zero"
    assert summarize_title_numbering([zero], title).nonstandard == 0


def test_fractional_episode_does_not_create_gap_between_fourteen_and_fifteen():
    title = CatalogTitle(
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type_manual="season",
    )
    items = videos(1, 15)
    fractional = Video(
        id=50, relative_path="Anime/Show/Season 1/Title 14.5.mkv",
        root_folder="Anime", filename="Title 14.5.mkv", size=1, mtime_ns=1,
    )
    items.append(fractional)

    recalculate_title_numbering(title, items)
    summary = summarize_title_numbering(items, title)

    assert fractional.local_episode_number is None
    assert fractional.season_episode_number is None
    assert fractional.episode_number_source == "fractional"
    assert summary.standard_total == 15
    assert summary.nonstandard == 1
    assert summary.gaps == ()


def test_structural_ab_variants_are_routed_to_review_without_canonical_duplicate():
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Re Zero/Season 1",
        part_type="season", season_number=1,
    )
    items = [
        Video(
            id=index,
            relative_path=f"Anime/Re Zero/Season 1/{filename}",
            root_folder="Anime",
            filename=filename,
            size=1,
            mtime_ns=1,
            catalog_title=title,
        )
        for index, filename in enumerate((
            "Re Zero kara Hajimeru Isekai Seikatsu - 01A.mkv",
            "Re Zero kara Hajimeru Isekai Seikatsu - 01B.mkv",
        ), 1)
    ]

    recalculate_title_numbering(title, items)
    summary = summarize_title_numbering(items, title)

    assert [detect_episode_number(video.filename).number for video in items] == [1, 1]
    assert [
        detect_episode_number(video.filename).filename_episode_hint for video in items
    ] == [1, 1]
    assert [
        detect_episode_number(video.filename).structural_marker for video in items
    ] == ["A", "B"]
    assert all(effective_video_numbering(video, title).is_nonstandard for video in items)
    assert all(video.local_episode_number is None for video in items)
    assert all(video.season_episode_number is None for video in items)
    assert {video.episode_number_source for video in items} == {"structural_variant"}
    assert summary.nonstandard == 2
    assert summary.requires_review is True
    assert unresolved_duplicate_groups(items) == ()


def test_fractional_episode_position_sorts_exactly_between_adjacent_integers():
    items = [
        Video(
            id=index,
            relative_path=f"Anime/Show/Season 1/{filename}",
            root_folder="Anime",
            filename=filename,
            size=1,
            mtime_ns=index,
            file_type="episode" if ".5" not in filename else "other",
        )
        for index, filename in enumerate(
            ("Show E06.mkv", "Show E05.5.mkv", "Show E05.mkv"), 1
        )
    ]

    assert [video.filename for video in sorted(items, key=video_sort_key)] == [
        "Show E05.mkv", "Show E05.5.mkv", "Show E06.mkv",
    ]
    explicitly_sorted, _, _ = sort_title_videos(items, "episode", "asc")
    assert [video.filename for video in explicitly_sorted] == [
        "Show E05.mkv", "Show E05.5.mkv", "Show E06.mkv",
    ]


def test_fractional_sxxexx_revision_stays_noncanonical_during_recalculation():
    title = CatalogTitle(
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1",
        part_type="season", season_number=1,
    )
    video = Video(
        id=1,
        relative_path="Anime/Show/Season 1/S01E14.5v2.mkv",
        root_folder="Anime",
        filename="S01E14.5v2.mkv",
        size=1,
        mtime_ns=1,
        catalog_title=title,
    )

    recalculate_title_numbering(title, [video])

    detection = detect_episode_number(video.filename)
    assert detection.display_value == "14.5"
    assert detection.season_hint == 1
    assert (
        video.local_episode_number,
        video.season_episode_number,
        video.absolute_episode_number,
        video.external_episode_number,
        video.episode_number_source,
    ) == (None, None, None, None, "fractional")


def test_classified_fractional_recap_is_resolved_without_entering_completeness():
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show", relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        collection=collection, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type_manual="season",
    )
    items = videos(1, 12)
    recap = Video(
        id=50, relative_path="Anime/Show/Season 1/Show 05.5.mkv",
        root_folder="Anime", filename="Show 05.5.mkv", size=1, mtime_ns=1,
        content_type_manual="recap",
    )
    items.append(recap)
    for item in items:
        item.catalog_title = title
        item.catalog_collection = collection

    recalculate_title_numbering(title, items)
    summary = summarize_title_numbering(items, title)

    assert summary.standard_total == 12
    assert summary.numbered == 12
    assert (summary.episode_min, summary.episode_max) == (1, 12)
    assert summary.gaps == ()
    assert summary.nonstandard == 0
    assert summary.resolved_supplemental == 1
    assert summary.requires_review is False
    assert (
        recap.local_episode_number,
        recap.season_episode_number,
        recap.absolute_episode_number,
        recap.external_episode_number,
    ) == (None, None, None, None)


def test_manual_supplementary_classification_clears_and_restores_derived_numbering():
    title = CatalogTitle(
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1",
        part_type="season", season_number=1,
    )
    video = Video(
        id=1,
        relative_path="Anime/Show/Season 1/Show E05.mkv",
        root_folder="Anime",
        filename="Show E05.mkv",
        size=1,
        mtime_ns=1,
        catalog_title=title,
    )

    recalculate_title_numbering(title, [video])
    assert (video.local_episode_number, video.season_episode_number) == (5, 5)

    video.content_type_manual = "recap"
    recalculate_title_numbering(title, [video])
    assert effective_video_numbering(video, title).is_supplementary
    assert (
        video.local_episode_number,
        video.season_episode_number,
        video.absolute_episode_number,
        video.external_episode_number,
    ) == (5, None, None, None)
    assert video.episode_number_source == "unknown"

    video.content_type_manual = None
    recalculate_title_numbering(title, [video])
    assert effective_video_numbering(video, title).is_standard
    assert (
        video.local_episode_number,
        video.season_episode_number,
        video.absolute_episode_number,
    ) == (5, 5, 5)


def test_numbered_ova_keeps_sequence_outside_standard_completeness():
    title = CatalogTitle(
        local_title="OVA – Serie 2", normalized_local_title="ova serie 2",
        relative_root_path="Anime/Show/.catalog-part-2", part_type="ova",
    )
    items = [Video(
        id=number, relative_path=f"Anime/Show/Show S2 - OVA P{number}.mkv",
        root_folder="Anime", filename=f"Show S2 - OVA P{number}.mkv",
        size=1, mtime_ns=1, catalog_title=title,
    ) for number in (1, 2)]

    recalculate_title_numbering(title, items)
    summary = summarize_title_numbering(items, title)

    assert (summary.numbered, summary.standard_total) == (0, 0)
    assert (summary.episode_min, summary.episode_max) == (None, None)
    assert summary.supplemental is True
    assert summary.requires_review is False
    assert [video.season_episode_number for video in items] == [None, None]


def test_standard_episode_and_ova_sequence_do_not_form_duplicate_group():
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season", season_number=1,
    )
    videos = [
        Video(
            id=1, relative_path="Anime/Show/Season 1/Title - 01.mkv",
            root_folder="Anime", filename="Title - 01.mkv", size=1, mtime_ns=1,
            catalog_title=title,
        ),
        Video(
            id=2, relative_path="Anime/Show/Season 1/Title - OVA 01.mkv",
            root_folder="Anime", filename="Title - OVA 01.mkv", size=1, mtime_ns=1,
            catalog_title=title,
        ),
    ]
    recalculate_title_numbering(title, videos)

    assert [video.season_episode_number for video in videos] == [1, None]
    assert unresolved_duplicate_groups(videos) == ()
    assert summarize_title_numbering(videos, title).standard_total == 1


@pytest.mark.parametrize(("filename", "expected_type", "expected_number"), (
    ("Title OVA - 01.mkv", "ova", 1),
    ("Title OVA Episode 01 Something.mkv", "ova", 1),
    ("Title OAD - 02.mkv", "ova", 2),
    ("Title Special - 03.mkv", "special", 3),
    ("Title PV - 04.mkv", "preview", 4),
    ("Title CM - 05.mkv", "cm", 5),
    ("Title Menu - 06.mkv", "menu", 6),
))
def test_safe_classifier_supplementary_type_outranks_generic_episode_number(
    filename, expected_type, expected_number,
):
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season", season_number=1,
    )
    relative_path = f"{title.relative_root_path}/{filename}"
    video = Video(
        id=1, relative_path=relative_path, root_folder="Anime", filename=filename,
        size=1, mtime_ns=1, catalog_title=title,
        file_type=classify_video(relative_path),
    )

    assert detect_episode_number(filename).kind == "standard"
    recalculate_title_numbering(title, [video])
    state = effective_video_numbering(video, title)

    assert state.is_supplementary
    assert state.supplementary_type == expected_type
    assert state.supplementary_number == expected_number
    assert (
        video.local_episode_number,
        video.season_episode_number,
        video.absolute_episode_number,
        video.external_episode_number,
    ) == (None, None, None, None)
    assert video.episode_number_source == f"supplementary_{expected_type}"


def test_classifier_only_ova_does_not_collide_with_regular_episode():
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season", season_number=1,
    )
    items = []
    for identifier, filename in enumerate((
        "Title - 01.mkv",
        "Title OVA - 01.mkv",
    ), 1):
        relative_path = f"{title.relative_root_path}/{filename}"
        items.append(Video(
            id=identifier,
            relative_path=relative_path,
            root_folder="Anime",
            filename=filename,
            size=1,
            mtime_ns=identifier,
            catalog_title=title,
            file_type=classify_video(relative_path),
        ))

    recalculate_title_numbering(title, items)

    regular, ova = items
    ova_state = effective_video_numbering(ova, title)
    assert regular.season_episode_number == 1
    assert ova.season_episode_number is None
    assert ova_state.is_supplementary
    assert (ova_state.supplementary_type, ova_state.supplementary_number) == ("ova", 1)
    assert unresolved_duplicate_groups(items) == ()
    summary = summarize_title_numbering(items, title)
    assert (summary.standard_total, summary.numbered, summary.resolved_supplemental) == (
        1, 1, 1,
    )


@pytest.mark.parametrize(("filename", "expected_type", "expected_number"), (
    ("Title OP 01.mkv", "op", 1),
    ("Title ED 02.mkv", "ed", 2),
    ("Title NCOP 01.mkv", "ncop", 1),
    ("Title NCED 01.mkv", "nced", 1),
))
def test_numbered_opening_and_ending_sequences_stay_supplementary(
    filename, expected_type, expected_number,
):
    title = CatalogTitle(
        id=1, local_title="NC", normalized_local_title="nc",
        relative_root_path="Anime/Show/NC", part_type="bonus",
    )
    relative_path = f"{title.relative_root_path}/{filename}"
    video = Video(
        id=1, relative_path=relative_path, root_folder="Anime", filename=filename,
        size=1, mtime_ns=1, catalog_title=title,
        file_type=classify_video(relative_path),
    )

    recalculate_title_numbering(title, [video])
    state = effective_video_numbering(video, title)

    assert state.detection.is_supplementary
    assert state.is_supplementary
    assert (state.supplementary_type, state.supplementary_number) == (
        expected_type, expected_number,
    )
    assert video.file_type == expected_type
    assert video.local_episode_number is None
    assert video.season_episode_number is None
    assert video.absolute_episode_number is None


def test_unmapped_other_file_type_does_not_override_standard_episode_number():
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season", season_number=1,
    )
    video = Video(
        id=1, relative_path="Anime/Show/Season 1/Title - 01.mkv",
        root_folder="Anime", filename="Title - 01.mkv", size=1, mtime_ns=1,
        catalog_title=title, file_type="other",
    )

    recalculate_title_numbering(title, [video])

    assert effective_video_numbering(video, title).is_standard
    assert video.season_episode_number == 1


@pytest.mark.parametrize(("filename", "expected_type"), [
    ("OP.mkv", "op"),
    ("ED.mkv", "ed"),
    ("Title OVA.mkv", "ova"),
    ("Title OAD.mkv", "ova"),
    ("Title Special.mkv", "special"),
    ("Title OP.mkv", "op"),
    ("Title ED.mkv", "ed"),
    ("Title NCOP.mkv", "ncop"),
    ("Title NCED.mkv", "nced"),
    ("Title PV.mkv", "preview"),
    ("Title Preview.mkv", "preview"),
    ("Title CM.mkv", "cm"),
    ("Title Menu.mkv", "menu"),
    ("Title Recap.mkv", "recap"),
    ("Title Bonus.mkv", "bonus"),
    ("Title Extras.mkv", "bonus"),
    ("Title [CM].mkv", "cm"),
    ("Title [PV].mkv", "preview"),
    ("Title [Menu].mkv", "menu"),
])
def test_explicit_supplementary_without_number_keeps_type_without_ordinal(
    filename, expected_type,
):
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season", season_number=1,
    )
    video = Video(
        id=1, relative_path=f"Anime/Show/Season 1/{filename}",
        root_folder="Anime", filename=filename, size=1, mtime_ns=1,
        catalog_title=title,
        file_type=classify_video(f"Anime/Show/Season 1/{filename}"),
    )

    recalculate_title_numbering(title, [video])

    state = effective_video_numbering(video, title)
    assert state.is_supplementary
    assert state.supplementary_type == expected_type
    assert state.supplementary_number is None
    assert FILE_TYPE_TO_SUPPLEMENTARY_SUBTYPE[video.file_type] == expected_type
    assert video.local_episode_number is None
    assert video.season_episode_number is None
    assert video.absolute_episode_number is None
    assert video.episode_number_source == f"supplementary_{expected_type}"


@pytest.mark.parametrize(("season_number", "season_label"), [
    (3, "S3"),
    (None, None),
])
def test_ova_part_keeps_optional_season_context_separate_from_video_numbering(
    season_number, season_label,
):
    title = CatalogTitle(
        id=1, local_title="OVA", normalized_local_title="ova",
        relative_root_path="Anime/Show/OVA", part_type="ova",
        season_number=season_number, season_label=season_label,
    )
    video = Video(
        id=1, relative_path="Anime/Show/OVA/Title OVA.mkv", root_folder="Anime",
        filename="Title OVA.mkv", size=1, mtime_ns=1, catalog_title=title,
        file_type=classify_video("Anime/Show/OVA/Title OVA.mkv"),
    )

    recalculate_title_numbering(title, [video])
    state = effective_video_numbering(video, title)
    summary = summarize_title_numbering([video], title)

    assert title.effective_part_type == "ova"
    assert title.effective_season_number == season_number
    assert title.effective_season_label == season_label
    assert state.is_supplementary
    assert state.supplementary_type == "ova"
    assert state.supplementary_number is None
    assert state.season_episode_number is None
    assert summary.supplemental is True
    assert summary.standard_total == 0
    assert summary.resolved_supplemental == 1


@pytest.mark.parametrize(
    ("filename", "expected_subtype", "expected_file_type", "expected_number"),
    [
        ("Title [CM01][codec].mkv", "cm", "cm", 1),
        ("Title [PV02][codec].mkv", "preview", "pv", 2),
        ("Title [Menu03][codec].mkv", "menu", "menu", 3),
        ("Title Recap 04.mkv", "recap", "recap", 4),
        ("Title Bonus 05.mkv", "bonus", "bonus", 5),
        ("Title Preview 06.mkv", "preview", "pv", 6),
    ],
)
def test_canonical_supplementary_mapping_keeps_ordinal_noncanonical(
    filename, expected_subtype, expected_file_type, expected_number,
):
    title = CatalogTitle(
        id=1, local_title="Extras", normalized_local_title="extras",
        relative_root_path="Anime/Show/Extras", part_type="bonus",
    )
    relative_path = f"{title.relative_root_path}/{filename}"
    video = Video(
        id=1, relative_path=relative_path, root_folder="Anime", filename=filename,
        size=1, mtime_ns=1, catalog_title=title,
        file_type=classify_video(relative_path),
    )

    recalculate_title_numbering(title, [video])
    state = effective_video_numbering(video, title)

    assert state.is_supplementary
    assert state.supplementary_type == expected_subtype
    assert state.supplementary_number == expected_number
    assert video.file_type == expected_file_type
    assert video.local_episode_number is None
    assert video.season_episode_number is None
    assert video.absolute_episode_number is None


def test_unknown_iv_marker_keeps_only_broad_bonus_context_and_is_noncanonical():
    title = CatalogTitle(
        id=1, local_title="Extras", normalized_local_title="extras",
        relative_root_path="Anime/Show/Extras", part_type="bonus",
    )
    filename = "Title [IV01][codec].mkv"
    relative_path = f"{title.relative_root_path}/{filename}"
    video = Video(
        id=1, relative_path=relative_path, root_folder="Anime", filename=filename,
        size=1, mtime_ns=1, catalog_title=title,
        file_type=classify_video(relative_path),
    )

    recalculate_title_numbering(title, [video])
    state = effective_video_numbering(video, title)

    assert state.is_supplementary
    assert state.detection.kind == "unknown"
    assert state.supplementary_type is None
    assert state.supplementary_number is None
    assert video.file_type == "other"
    assert video.local_episode_number is None
    assert video.season_episode_number is None
    assert video.absolute_episode_number is None


def test_manual_episode_override_outranks_classifier_supplementary_fallback():
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season", season_number=1,
    )
    video = Video(
        id=1, relative_path="Anime/Show/Season 1/Title OVA - 01.mkv",
        root_folder="Anime", filename="Title OVA - 01.mkv", size=1, mtime_ns=1,
        catalog_title=title, file_type="ova", episode_number_manual_override=7,
    )

    recalculate_title_numbering(title, [video])

    state = effective_video_numbering(video, title)
    assert state.is_standard
    assert state.manual_override
    assert video.season_episode_number == 7
    assert video.episode_number_source == "manual"


@pytest.mark.parametrize(("filename", "kind", "version_hint", "marker"), (
    ("Overlord - 01 - End and Beginning.mkv", "standard", None, None),
    ("Kaifuku Jutsushi no Yarinaoshi - 01 (UC).mkv", "standard", "UC", None),
    ("Nande Koko ni Sensei ga! - 01 Ver.TV.mp4", "standard", "Ver.TV", None),
    (
        "Re Zero kara Hajimeru Isekai Seikatsu - 01A.mkv",
        "structural_variant", None, "A",
    ),
    (
        "Re Zero kara Hajimeru Isekai Seikatsu - 01B.mkv",
        "structural_variant", None, "B",
    ),
))
def test_commit_one_parser_results_survive_supplementary_authority(
    filename, kind, version_hint, marker,
):
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season", season_number=1,
    )
    relative_path = f"{title.relative_root_path}/{filename}"
    video = Video(
        id=1, relative_path=relative_path, root_folder="Anime", filename=filename,
        size=1, mtime_ns=1, catalog_title=title,
        file_type=classify_video(relative_path),
    )

    recalculate_title_numbering(title, [video])
    detection = detect_episode_number(filename)
    state = effective_video_numbering(video, title)

    assert detection.kind == kind
    assert detection.version_hint == version_hint
    assert detection.structural_marker == marker
    if kind == "standard":
        assert state.is_standard
        assert video.season_episode_number == 1
    else:
        assert state.is_nonstandard
        assert video.season_episode_number is None


def test_sxxexx_uses_specific_source_and_sp_hint_stays_outside_standard_numbering():
    title = CatalogTitle(
        local_title="Serie 1", normalized_local_title="serie 1",
        relative_root_path="Anime/Hataraku Saibou/Serie 1",
        part_type="season", season_number=1,
    )
    episode = Video(
        relative_path=f"{title.relative_root_path}/S01E01-Pneumococcus.mkv",
        root_folder="Anime", filename="S01E01-Pneumococcus.mkv",
        size=1, mtime_ns=1, catalog_title=title,
    )
    special = Video(
        relative_path=f"{title.relative_root_path}/S01E14 [SP]-The Common Cold.mkv",
        root_folder="Anime", filename="S01E14 [SP]-The Common Cold.mkv",
        size=1, mtime_ns=1, catalog_title=title,
    )

    recalculate_title_numbering(title, [episode, special])

    assert (
        episode.local_episode_number, episode.season_episode_number,
        episode.episode_number_source,
    ) == (1, 1, "sxxexx")
    assert (
        special.local_episode_number, special.season_episode_number,
        special.absolute_episode_number, special.external_episode_number,
    ) == (None, None, None, None)
    assert special.episode_number_source == "supplementary_special"
    summary = summarize_title_numbering([episode, special], title)
    assert (summary.standard_total, summary.numbered, summary.resolved_supplemental) == (
        1, 1, 1,
    )


def test_manual_number_can_be_canonical_special_number_without_reusing_filename_hint():
    title = CatalogTitle(
        local_title="Specials – S1", normalized_local_title="specials s1",
        relative_root_path="Anime/Hataraku Saibou/.catalog-part-specials",
        part_type_manual="special", season_number_manual=1,
        hierarchy_manual_override=True,
    )
    special = Video(
        relative_path="Anime/Hataraku Saibou/Serie 1/S01E14 [SP]-The Common Cold.mkv",
        root_folder="Anime", filename="S01E14 [SP]-The Common Cold.mkv",
        size=1, mtime_ns=1, catalog_title=title,
        content_type_manual="special",
        episode_number_manual_override=1,
    )

    recalculate_title_numbering(title, [special])

    assert special.local_episode_number is None
    assert special.season_episode_number == 1
    assert special.episode_number_source == "manual"
    assert summarize_title_numbering([special], title).supplemental is True


def test_stale_supplementary_episode_value_is_ignored_by_read_only_summary():
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season", season_number=1,
    )
    videos = [
        Video(
            id=1, relative_path="Anime/Show/Season 1/Title - 01.mkv",
            root_folder="Anime", filename="Title - 01.mkv", size=1, mtime_ns=1,
            local_episode_number=1, season_episode_number=1, catalog_title=title,
        ),
        Video(
            id=2, relative_path="Anime/Show/Season 1/Title - Special 01.mkv",
            root_folder="Anime", filename="Title - Special 01.mkv", size=1, mtime_ns=1,
            local_episode_number=1, season_episode_number=1, catalog_title=title,
        ),
    ]

    summary = summarize_title_numbering(videos, title)

    assert (summary.standard_total, summary.numbered) == (1, 1)
    assert summary.duplicate_numbers == ()
    assert summary.resolved_supplemental == 1


def _supplementary_video(identifier, path, filename, title):
    return Video(
        id=identifier, relative_path=path, root_folder="Anime", filename=filename,
        size=identifier, mtime_ns=1, catalog_title=title,
        catalog_collection=title.collection,
        catalog_title_id=title.id, catalog_collection_id=title.collection.id,
    )


def test_supplementary_duplicate_identity_respects_subtype_and_season_context():
    collection = CatalogCollection(
        id=1, local_title="High School DxD", normalized_local_title="high school dxd",
        relative_root_path="Anime/High School DxD",
    )
    nc = CatalogTitle(
        id=1, collection=collection, local_title="NC", normalized_local_title="nc",
        relative_root_path="Anime/High School DxD/NC", part_type="bonus",
    )
    videos = [
        _supplementary_video(
            1, "Anime/High School DxD/NC/Season 2/OP 02.mkv", "OP 02.mkv", nc,
        ),
        _supplementary_video(
            2, "Anime/High School DxD/NC/Season 3/OP 02.mkv", "OP 02.mkv", nc,
        ),
        _supplementary_video(
            3, "Anime/High School DxD/NC/Season 2/ED 02.mkv", "ED 02.mkv", nc,
        ),
    ]

    assert unresolved_duplicate_groups(videos) == ()


def test_two_physical_copies_of_same_supplementary_identity_are_candidate_duplicate():
    collection = CatalogCollection(
        id=1, local_title="High School DxD", normalized_local_title="high school dxd",
        relative_root_path="Anime/High School DxD",
    )
    nc = CatalogTitle(
        id=1, collection=collection, local_title="NC – S2", normalized_local_title="nc s2",
        relative_root_path="Anime/High School DxD/NC/Season 2", part_type="bonus",
        season_number=2, season_label="S2",
    )
    videos = [
        _supplementary_video(
            1, "Anime/High School DxD/NC/Season 2/release-a/OP 01.mkv", "OP 01.mkv", nc,
        ),
        _supplementary_video(
            2, "Anime/High School DxD/NC/Season 2/release-b/OP 01.mkv", "OP 01.mkv", nc,
        ),
    ]

    groups = unresolved_duplicate_groups(videos)

    assert len(groups) == 1
    assert groups[0].display_label == "OP 01 · S2"
    assert groups[0].videos == tuple(videos)

    set_duplicate_group_primary(videos, videos[0])

    confirmed = confirmed_duplicate_groups(videos)
    assert len(confirmed) == 1
    assert confirmed[0].display_label == "OP 01 · S2"
    assert confirmed[0].primary is videos[0]


def test_specials_filename_prefix_maps_to_existing_season_title_context():
    collection = CatalogCollection(
        id=1, local_title="High School DxD", normalized_local_title="high school dxd",
        relative_root_path="Anime/High School DxD",
    )
    born = CatalogTitle(
        id=1, collection=collection, local_title="High School DxD Born (J15)",
        normalized_local_title="high school dxd born j15",
        relative_root_path="Anime/High School DxD/Born", part_type="season",
        season_number=3, season_label="S3",
    )
    specials = CatalogTitle(
        id=2, collection=collection, local_title="Specials",
        normalized_local_title="specials",
        relative_root_path="Anime/High School DxD/Specials", part_type="special",
    )
    videos = [
        _supplementary_video(
            1, "Anime/High School DxD/Specials/Born A.mkv",
            "High School DxD Born - Special 01.mkv", specials,
        ),
        _supplementary_video(
            2, "Anime/High School DxD/Specials/Born B.mkv",
            "High School DxD Born - Special 01.mkv", specials,
        ),
    ]

    groups = unresolved_duplicate_groups(videos)

    assert len(groups) == 1
    assert groups[0].display_label == "Special 01 · S3"
    assert born.id == 1


def _bungo_duplicate_videos():
    collection = CatalogCollection(
        id=1, local_title="Bungo to Alchemist - Shinpan no Haguruma",
        normalized_local_title="bungo to alchemist shinpan no haguruma",
        relative_root_path="Anime/Bungo",
    )
    title = CatalogTitle(
        id=10, collection=collection, local_title="Season 1",
        normalized_local_title="season 1", relative_root_path="Anime/Bungo/Season 1",
        part_type="season", season_number=1,
    )
    items = []
    for number in range(1, 14):
        for copy, extension in (("A", "mkv"), ("B", "mp4")):
            items.append(Video(
                id=number * 10 + (copy == "B"),
                relative_path=f"Anime/Bungo/Season 1/Bungo {number:02} {copy}.{extension}",
                root_folder="Anime", filename=f"Bungo {number:02}.{extension}",
                size=number, mtime_ns=1, season_episode_number=number,
                catalog_title_id=title.id, catalog_collection_id=collection.id,
                catalog_title=title, catalog_collection=collection,
            ))
    return collection, title, items


def test_bungo_duplicate_groups_change_from_unresolved_to_confirmed_physical_warning():
    _, title, items = _bungo_duplicate_videos()

    before = summarize_title_numbering(items, title)
    assert before.total == 26
    assert before.standard_total == 26
    assert before.duplicate_numbers == tuple(range(1, 14))
    assert len(unresolved_duplicate_groups(items)) == 13
    assert before.requires_review is True

    for group in unresolved_duplicate_groups(items):
        set_duplicate_group_primary(list(group.videos), group.videos[0])

    after = summarize_title_numbering(items, title)
    assert after.total == 26
    assert after.standard_total == 13
    assert after.numbered == 13
    assert (after.episode_min, after.episode_max) == (1, 13)
    assert after.gaps == ()
    assert after.duplicate_numbers == ()
    assert after.confirmed_duplicates == 13
    assert len(unresolved_duplicate_groups(items)) == 0
    assert len(confirmed_duplicate_groups(items)) == 13
    assert after.requires_review is False


def test_duplicate_primary_is_explicit_changeable_and_cannot_be_self_reference():
    _, _, items = _bungo_duplicate_videos()
    first_group = list(unresolved_duplicate_groups(items)[0].videos)
    first, second = first_group
    paths = [(video.filename, video.relative_path) for video in first_group]

    set_duplicate_group_primary(first_group, first)
    assert second.duplicate_of is first
    assert first.duplicate_of is None

    set_duplicate_group_primary(first_group, second)
    assert first.duplicate_of is second
    assert second.duplicate_of is None
    assert second not in first.duplicate_copies
    assert [(video.filename, video.relative_path) for video in first_group] == paths

    with pytest.raises(ValueError, match="stejné video"):
        set_duplicate_group_primary([first, first], first)


def test_multiple_duplicate_copies_can_share_one_primary_without_cycle():
    collection, title, items = _bungo_duplicate_videos()
    primary, second = list(unresolved_duplicate_groups(items)[0].videos)
    third = Video(
        id=999, relative_path="Anime/Bungo/Season 1/Bungo 01 third.avi",
        root_folder="Anime", filename="Bungo 01.avi", size=1, mtime_ns=1,
        season_episode_number=1, catalog_title_id=title.id,
        catalog_collection_id=collection.id, catalog_title=title,
        catalog_collection=collection,
    )

    set_duplicate_group_primary([primary, second, third], primary)

    assert second.duplicate_of is primary
    assert third.duplicate_of is primary
    assert primary.duplicate_of is None
    assert set(primary.duplicate_copies) == {second, third}
