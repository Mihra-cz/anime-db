from app.models import CatalogTitle, TitleMetadata, Video
from app.numbering import (
    recalculate_title_numbering, set_title_numbering, set_video_episode_override,
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
