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


def test_season_folders_share_one_anime_collection():
    paths = [
        "Anime/Anime XYZ/Season 1/E01.mkv",
        "Anime/Anime XYZ/Season 2/E01.mkv",
    ]
    hierarchy = derive_library_hierarchy(paths)

    assert {item.collection.relative_root_path for item in hierarchy.values()} == {
        "Anime/Anime XYZ"
    }
    assert {item.title.local_title for item in hierarchy.values()} == {
        "Season 1", "Season 2"
    }


@pytest.mark.parametrize(("folder", "part_type"), [
    ("OVA", "ova"),
    ("Specials", "special"),
    ("NC", "bonus"),
    ("NCOP", "bonus"),
    ("OP", "bonus"),
    ("ED", "bonus"),
    ("Movies", "film"),
])
def test_supplementary_child_is_a_title_of_parent_anime(folder, part_type):
    path = f"Anime/Anime XYZ/{folder}/item.mkv"
    identity = derive_library_hierarchy([path])[path]

    assert identity.collection.relative_root_path == "Anime/Anime XYZ"
    assert identity.title.relative_root_path == f"Anime/Anime XYZ/{folder}"
    assert identity.title.part_type == part_type


def test_nested_nc_remains_under_anime_root_but_has_own_title():
    paths = [
        "Anime/Anime XYZ/Season 1/E01.mkv",
        "Anime/Anime XYZ/Season 1/NC/Opening.mkv",
    ]
    hierarchy = derive_library_hierarchy(paths)

    assert {item.collection.relative_root_path for item in hierarchy.values()} == {
        "Anime/Anime XYZ"
    }
    assert hierarchy[paths[1]].title.relative_root_path == "Anime/Anime XYZ/Season 1/NC"
    assert hierarchy[paths[1]].title.part_type == "bonus"


def test_nc_named_season_child_preserves_context_as_separate_title():
    paths = [
        "Anime/High School DxD/NC/High School DxD New/ED 02.mkv",
        "Anime/High School DxD/NC/High School DxD Born/OP 02.mkv",
        "Anime/High School DxD/NC/High School DxD Hero/OP 02.mkv",
    ]
    hierarchy = derive_library_hierarchy(paths)

    assert {item.collection.relative_root_path for item in hierarchy.values()} == {
        "Anime/High School DxD"
    }
    assert {item.title.local_title for item in hierarchy.values()} == {
        "NC – High School DxD New",
        "NC – High School DxD Born",
        "NC – High School DxD Hero",
    }
    assert len({item.title.relative_root_path for item in hierarchy.values()}) == 3
    assert {item.title.detection_reason for item in hierarchy.values()} == {
        "supplementary_named_child"
    }


def test_film_bonus_folder_is_not_a_main_collection():
    paths = [
        "Anime/Tenki no Ko (FILM)/Tenki no Ko.mkv",
        "Anime/Tenki no Ko (FILM)/CM&PV/Trailer.mkv",
    ]
    hierarchy = derive_library_hierarchy(paths)

    assert {item.collection.relative_root_path for item in hierarchy.values()} == {
        "Anime/Tenki no Ko (FILM)"
    }
    assert hierarchy[paths[0]].title.part_type == "film"
    assert hierarchy[paths[1]].title.part_type == "bonus"


def test_related_named_season_uses_parent_as_collection_with_reviewable_type():
    path = "Anime/High School DxD (Z12-J18)/High School DxD Born (J15)/E01.mkv"
    identity = derive_library_hierarchy([path])[path]

    assert identity.collection.relative_root_path == "Anime/High School DxD (Z12-J18)"
    assert identity.title.local_title == "High School DxD Born (J15)"
    assert identity.title.part_type == "title"
    assert identity.title.detection_reason == "related_named_child"


def test_similar_name_without_shared_anime_parent_is_not_grouped():
    paths = [
        "Anime/High School DxD/E01.mkv",
        "Anime/High School DxD Born/E01.mkv",
    ]
    hierarchy = derive_library_hierarchy(paths)

    assert len({item.collection.relative_root_path for item in hierarchy.values()}) == 2


def test_mob_psycho_reference_structure_stays_one_collection():
    paths = [
        "Anime/Mob Psycho 100/Season 1/E01.mkv",
        "Anime/Mob Psycho 100/Season 2/E01.mkv",
        "Anime/Mob Psycho 100/Season 3/E01.mkv",
        "Anime/Mob Psycho 100/OVA/OVA 01.mkv",
    ]
    hierarchy = derive_library_hierarchy(paths)

    assert {item.collection.relative_root_path for item in hierarchy.values()} == {
        "Anime/Mob Psycho 100"
    }
    assert len({item.title.relative_root_path for item in hierarchy.values()}) == 4
