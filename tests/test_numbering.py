import pytest

from app.catalog import detect_episode_number
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
        part_number=number, season_number=number, episode_start_offset=offset,
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


def test_numbered_ova_keeps_sequence_outside_standard_completeness():
    title = CatalogTitle(
        local_title="OVA – Serie 2", normalized_local_title="ova serie 2",
        relative_root_path="Anime/Show/.catalog-part-2", part_type_manual="ova",
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
