import pytest

from app.hierarchy import derive_library_hierarchy


def test_ansatsu_season_folders_create_two_parts_in_one_collection():
    paths = [
        "Anime/Ansatsu Kyoushitsu (Z15-Z16)/Serie 1 (Z15)/E01.mkv",
        "Anime/Ansatsu Kyoushitsu (Z15-Z16)/Serie 2 (Z16)/E01.mkv",
    ]
    hierarchy = derive_library_hierarchy(paths)
    first, second = hierarchy[paths[0]], hierarchy[paths[1]]
    assert first.collection == second.collection
    assert first.collection.local_title == "Ansatsu Kyoushitsu (Z15-Z16)"
    assert first.title.local_title == "Serie 1 (Z15)"
    assert first.title.season_number == 1 and first.title.season_label == "S1"
    assert second.title.season_number == 2 and second.title.season_label == "S2"


@pytest.mark.parametrize(("roman", "number"), [
    ("I", 1), ("II", 2), ("III", 3), ("IV", 4),
])
def test_overlord_roman_siblings_are_contextual_seasons(roman, number):
    paths = [
        f"Anime/OVERLORD (L15-L22)/OVERLORD {value}/E01.mkv"
        for value in ("I", "II", "III", "IV")
    ]
    hierarchy = derive_library_hierarchy(paths)
    selected = hierarchy[f"Anime/OVERLORD (L15-L22)/OVERLORD {roman}/E01.mkv"]
    assert selected.collection.local_title == "OVERLORD (L15-L22)"
    assert selected.title.season_number == number
    assert selected.title.season_label == f"S{number}"
    assert selected.title.normalized_base == "overlord"
    assert selected.title.detection_reason == "roman_sibling_same_base"


def test_roman_suffix_is_not_a_season_without_sibling_context():
    path = "Anime/Legend II/E01.mkv"
    identity = derive_library_hierarchy([path])[path]
    assert identity.collection.local_title == "Legend II"
    assert identity.title.season_number is None


def test_video_directly_in_anime_root_has_single_unseasoned_title():
    path = "Anime/Standalone/E01.mkv"
    identity = derive_library_hierarchy([path])[path]
    assert identity.collection.local_title == "Standalone"
    assert identity.title.local_title == "Standalone"
    assert identity.title.season_number is None
    assert identity.title.season_label is None


@pytest.mark.parametrize(("folder", "number"), [
    ("1st Season", 1), ("2nd Season", 2), ("Third Season", 3),
    ("Part 2", 2), ("Cour 1", 1), ("S01", 1),
])
def test_supported_numbered_part_names(folder, number):
    path = f"Anime/Show/{folder}/E01.mkv"
    identity = derive_library_hierarchy([path])[path]
    assert identity.title.season_number == number
    assert identity.title.original_folder_name == folder
