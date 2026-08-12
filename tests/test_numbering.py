import pytest

from app.models import CatalogCollection, CatalogTitle, TitleMetadata, Video
from app.numbering import (
    apply_sequential_numbering, collection_requires_numbering_review,
    preview_sequential_numbering, recalculate_collection_numbering,
    recalculate_title_numbering,
    set_title_numbering, set_video_episode_override, summarize_title_numbering,
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


def test_numbered_ova_shows_range_but_does_not_use_season_completeness():
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

    assert (summary.numbered, summary.standard_total) == (2, 2)
    assert (summary.episode_min, summary.episode_max) == (1, 2)
    assert summary.supplemental is True
    assert summary.requires_review is False
