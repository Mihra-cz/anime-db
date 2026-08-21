import pytest

from app.hierarchy_review import (
    FILENAME_SEASON_CONFLICT_REVIEW_REASON, apply_single_title_confirmation,
    collection_requires_review,
    refresh_collection_state, single_title_confirmation_suggestion,
)
from app.hierarchy_types import PART_TYPES
from app.models import CatalogCollection, CatalogTitle, Video, utc_now
from app.numbering import summarize_title_numbering
from app.structural_inference import (
    GENERIC_TITLE_REVIEW_REASON, LONG_FLAT_SEQUENCE_REVIEW_REASON,
    automatic_flat_sequence_notice,
    direct_root_episode_profile,
)


def direct_root_collection(count: int) -> tuple[CatalogCollection, CatalogTitle]:
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show", hierarchy_status="automatic",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="Show",
        normalized_local_title="show", relative_root_path="Anime/Show",
        part_type="title", hierarchy_manual_override=False,
    )
    for number in range(1, count + 1):
        Video(
            id=number, relative_path=f"Anime/Show/Show - {number:02}.mkv",
            root_folder="Anime", filename=f"Show - {number:02}.mkv",
            size=1, mtime_ns=number, catalog_title=title,
            catalog_collection=collection,
        )
    return collection, title


@pytest.mark.parametrize("count", [12, 14])
def test_short_direct_root_sequence_is_automatic_season_one_without_warning(count):
    collection, title = direct_root_collection(count)

    refresh_collection_state(collection)

    assert (title.part_type, title.season_number, title.season_label) == (
        "season", 1, "S1",
    )
    assert title.part_type_manual is None
    assert title.season_number_manual is None
    assert title.season_label_manual is None
    assert title.hierarchy_manual_override is False
    assert title.hierarchy_verified_at is None
    assert collection.hierarchy_status == "automatic"
    assert collection.hierarchy_note is None
    assert collection.hierarchy_verified_at is None
    assert automatic_flat_sequence_notice(title) is None


@pytest.mark.parametrize("count", [15, 24])
def test_medium_direct_root_sequence_has_only_derived_soft_warning(count):
    collection, title = direct_root_collection(count)

    refresh_collection_state(collection)

    assert title.effective_part_type == "season"
    assert title.effective_season_number == 1
    assert collection.hierarchy_status == "automatic"
    assert collection.hierarchy_note is None
    assert automatic_flat_sequence_notice(title) == (
        f"Delší souvislá řada E1–E{count} bez explicitního dělení. Zkontrolujte "
        "případné rozdělení na sezóny nebo části."
    )


@pytest.mark.parametrize("count", [25, 40])
def test_very_long_direct_root_sequence_is_not_split_but_requires_review(count):
    collection, title = direct_root_collection(count)

    refresh_collection_state(collection)

    assert (title.part_type, title.season_number, title.season_label) == (
        "season", 1, "S1",
    )
    assert len(collection.titles) == 1
    assert collection.hierarchy_status == "review_required"
    assert collection.hierarchy_note == LONG_FLAT_SEQUENCE_REVIEW_REASON
    assert automatic_flat_sequence_notice(title) is None


def test_video_level_supplementary_content_does_not_increase_flat_sequence_length():
    collection, title = direct_root_collection(12)
    next_id = 13
    for filename, manual_type in (
        ("Show - 04.5.mkv", "recap"),
        ("Show OVA 01.mkv", None),
        ("Show Bonus 01.mkv", None),
    ):
        Video(
            id=next_id, relative_path=f"Anime/Show/{filename}",
            root_folder="Anime", filename=filename, size=1, mtime_ns=next_id,
            content_type_manual=manual_type, catalog_title=title,
            catalog_collection=collection,
        )
        next_id += 1

    refresh_collection_state(collection)
    profile = direct_root_episode_profile(list(title.videos))
    summary = summarize_title_numbering(list(title.videos), title)

    assert profile.standard_count == 12
    assert summary.standard_total == 12
    assert summary.resolved_supplemental == 3
    assert collection.hierarchy_status == "automatic"
    assert automatic_flat_sequence_notice(title) is None


def test_confirmed_duplicate_copy_does_not_increase_flat_sequence_length():
    collection, title = direct_root_collection(14)
    duplicate = Video(
        id=15, relative_path="Anime/Show/Show - 14 alternate.mkv",
        root_folder="Anime", filename="Show - 14 alternate.mkv",
        size=1, mtime_ns=15, duplicate_of=title.videos[-1],
        catalog_title=title, catalog_collection=collection,
    )

    profile = direct_root_episode_profile(list(title.videos))

    assert duplicate.duplicate_of is title.videos[-2]
    assert profile.standard_count == 14
    assert automatic_flat_sequence_notice(title) is None


def test_explicit_season_two_with_25_episodes_has_no_flat_length_signal():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show", hierarchy_status="automatic",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="Season 2",
        normalized_local_title="season 2",
        relative_root_path="Anime/Show/Season 2", part_type="season",
        season_number=2, season_label="S2",
    )
    for number in range(1, 26):
        Video(
            id=number, relative_path=f"Anime/Show/Season 2/Show - {number:02}.mkv",
            root_folder="Anime", filename=f"Show - {number:02}.mkv",
            size=1, mtime_ns=number, catalog_title=title,
            catalog_collection=collection,
        )

    refresh_collection_state(collection)

    assert title.effective_part_type == "season"
    assert title.effective_season_number == 2
    assert collection.hierarchy_status == "automatic"
    assert collection.hierarchy_note is None
    assert automatic_flat_sequence_notice(title) is None


def test_single_unmarked_video_stays_technical_title_and_requires_review():
    collection, title = direct_root_collection(1)

    refresh_collection_state(collection)

    assert title.effective_part_type == "title"
    assert collection.hierarchy_status == "review_required"
    assert collection.hierarchy_note == GENERIC_TITLE_REVIEW_REASON


def test_single_unknown_video_does_not_get_an_unsafe_season_one_suggestion():
    collection = CatalogCollection(
        id=1, local_title="Unknown", normalized_local_title="unknown",
        relative_root_path="Anime/Unknown", hierarchy_status="review_required",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="Unknown",
        normalized_local_title="unknown", relative_root_path="Anime/Unknown",
    )
    Video(
        id=1, relative_path="Anime/Unknown/Unknown.mkv", root_folder="Anime",
        filename="Unknown.mkv", size=1, mtime_ns=1, catalog_title=title,
        catalog_collection=collection,
    )

    refresh_collection_state(collection)

    assert title.effective_part_type == "title"
    assert collection.hierarchy_status == "review_required"
    assert single_title_confirmation_suggestion(collection) is None


def test_manual_structural_override_is_never_replaced_by_automatic_inference():
    collection, title = direct_root_collection(12)
    title.part_type_manual = "ova"
    title.hierarchy_manual_override = True
    title.hierarchy_verified_at = utc_now()

    refresh_collection_state(collection)

    assert title.effective_part_type == "ova"
    assert title.part_type_manual == "ova"
    assert title.hierarchy_manual_override is True
    assert collection.hierarchy_status == "verified"


def test_s02_filename_inside_automatic_s1_remains_a_blocking_conflict():
    collection, title = direct_root_collection(12)
    Video(
        id=13, relative_path="Anime/Show/Show S02E13.mkv",
        root_folder="Anime", filename="Show S02E13.mkv", size=1, mtime_ns=13,
        catalog_title=title, catalog_collection=collection,
    )

    refresh_collection_state(collection)

    assert title.effective_part_type == "season"
    assert title.effective_season_number == 1
    assert collection_requires_review(collection, list(collection.videos)) == (
        FILENAME_SEASON_CONFLICT_REVIEW_REASON
    )
    assert collection.hierarchy_status == "review_required"


def test_technical_title_is_not_an_authoritative_manual_choice():
    collection, _ = direct_root_collection(12)
    assert "title" not in PART_TYPES

    with pytest.raises(ValueError, match="Neplatný typ části"):
        apply_single_title_confirmation(
            collection, part_type="title", season_number=None, season_label=None,
        )
