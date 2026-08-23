import pytest

from app.catalog import catalog_title_series_label
from app.media_parts import (
    duplicate_media_part_ordinals, media_part_label,
    media_part_ordinal_warning, media_part_sequence_warning,
    media_part_summary_label, media_part_total, set_media_part_number,
)
from app.models import CatalogCollection, CatalogTitle, TitleMetadata, Video


def video(number: int | None, *, filename: str = "Movie.mkv") -> Video:
    return Video(
        relative_path=f"Anime/Movie/{filename}", root_folder="Anime",
        filename=filename, size=1, mtime_ns=1, media_part_number=number,
    )


def test_media_part_number_can_be_set_changed_and_cleared():
    item = video(None)

    assert item.media_part_number is None
    assert set_media_part_number(item, 1).media_part_number == 1
    assert set_media_part_number(item, 2).media_part_number == 2
    assert set_media_part_number(item, None).media_part_number is None


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "1"])
def test_media_part_number_rejects_nonpositive_or_noninteger_values(invalid):
    item = video(None)

    with pytest.raises(ValueError, match="kladné celé číslo"):
        set_media_part_number(item, invalid)

    assert item.media_part_number is None


def test_media_part_display_requires_a_complete_contiguous_set_for_total():
    single = [video(1, filename="One.mkv")]
    only_two = [video(2, filename="Two.mkv")]
    pair = [video(1, filename="One.mkv"), video(2, filename="Two.mkv")]
    triple = [
        video(1, filename="One.mkv"), video(2, filename="Two.mkv"),
        video(3, filename="Three.mkv"),
    ]
    gap = [video(1, filename="One.mkv"), video(3, filename="Three.mkv")]

    assert media_part_label(video(None), single) is None
    assert media_part_total(single) is None
    assert media_part_label(single[0], single) == "Část média 1"
    assert media_part_label(only_two[0], only_two) == "Část média 2"
    assert media_part_sequence_warning(only_two) is not None
    assert [media_part_label(item, pair) for item in pair] == [
        "Část média 1/2", "Část média 2/2",
    ]
    assert [media_part_label(item, triple) for item in triple] == [
        "Část média 1/3", "Část média 2/3", "Část média 3/3",
    ]
    assert [media_part_label(item, gap) for item in gap] == [
        "Část média 1", "Část média 3",
    ]
    assert media_part_summary_label(pair) == "2 části média"
    assert media_part_summary_label(triple) == "3 části média"
    assert media_part_summary_label(gap) is None
    assert media_part_sequence_warning(single) is None
    assert media_part_sequence_warning(gap) is not None


def test_confirmed_secondary_duplicate_does_not_increase_total_or_conflict():
    primary_one = video(1, filename="One.mkv")
    primary_two = video(2, filename="Two.mkv")
    duplicate_one = video(1, filename="One copy.mkv")
    duplicate_one.duplicate_of = primary_one
    siblings = [primary_one, primary_two, duplicate_one]

    assert media_part_total(siblings) == 2
    assert duplicate_media_part_ordinals(siblings) == ()
    assert media_part_label(primary_one, siblings) == "Část média 1/2"
    assert media_part_label(primary_two, siblings) == "Část média 2/2"
    assert media_part_label(duplicate_one, siblings) == "Část média 1/2"


def test_duplicate_active_primary_ordinal_has_local_diagnostic():
    first = video(1, filename="First.mkv")
    second = video(1, filename="Second.mkv")
    third = video(2, filename="Third.mkv")
    siblings = [first, second, third]

    assert duplicate_media_part_ordinals(siblings) == (1,)
    assert media_part_total(siblings) is None
    assert media_part_sequence_warning(siblings) is None
    assert "více aktivních primárních videí" in media_part_ordinal_warning(
        first, siblings,
    )
    assert media_part_ordinal_warning(second, siblings) is not None


def test_media_part_is_independent_of_hierarchy_numbering_and_metadata():
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show", hierarchy_status="automatic",
        hierarchy_note=None,
    )
    metadata = TitleMetadata(display_title="Show Season 1 Part 2")
    title = CatalogTitle(
        collection=collection, local_title="Part 2",
        normalized_local_title="part 2",
        relative_root_path="Anime/Show/Season 1/Part 2",
        part_type="part", season_number=1, part_number=2,
        season_label="S1", metadata_record=metadata,
    )
    item = Video(
        relative_path=f"{title.relative_root_path}/E04.mkv",
        root_folder="Anime", filename="E04.mkv", size=1, mtime_ns=1,
        season_episode_number=4, absolute_episode_number=4,
        catalog_title=title, catalog_collection=collection,
    )
    before = (
        collection.hierarchy_status, collection.hierarchy_note,
        title.part_type, title.part_number, title.part_number_manual,
        title.season_number, title.season_number_manual, title.numbering_mode,
        item.season_episode_number, item.absolute_episode_number,
        title.metadata_record,
    )

    set_media_part_number(item, 1)

    assert item.media_part_number == 1
    assert catalog_title_series_label(title) == "S1 · Part 2"
    assert (
        collection.hierarchy_status, collection.hierarchy_note,
        title.part_type, title.part_number, title.part_number_manual,
        title.season_number, title.season_number_manual, title.numbering_mode,
        item.season_episode_number, item.absolute_episode_number,
        title.metadata_record,
    ) == before
