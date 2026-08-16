import pytest

from app.metadata.service import default_metadata_search_query, normalize_metadata_search_query
from app.models import CatalogCollection, CatalogTitle, TitleMetadata


@pytest.mark.parametrize(("local_title", "expected"), [
    ("OVERLORD (J19)", "OVERLORD"),
    ("Ansatsu Kyoushitsu (Z15-Z16)", "Ansatsu Kyoushitsu"),
    ("OVERLORD (L15-L22)", "OVERLORD"),
    ("Darker than Black (J07-P09)", "Darker than Black"),
    (
        "Kono Yo no Hate de Koi wo Utau Shoujo YU-NO J19 cz-xx%",
        "Kono Yo no Hate de Koi wo Utau Shoujo YU-NO",
    ),
])
def test_removes_known_trailing_library_annotations(local_title, expected):
    assert normalize_metadata_search_query(local_title) == expected


@pytest.mark.parametrize("title", [
    "Anime (TV)",
    "Title (Second Season)",
    "Title (J19) pokračování",
    "Steins;Gate 0",
    "Re:Zero",
    "Fate/stay night",
    "K-On!",
    "Working!!",
    "Evangelion: 3.0+1.0",
])
def test_preserves_real_title_content(title):
    assert normalize_metadata_search_query(title) == title


def test_normalization_never_changes_catalog_title():
    title = CatalogTitle(
        local_title="OVERLORD (L15-L22)", normalized_local_title="overlord l15 l22",
        relative_root_path="Anime/OVERLORD (L15-L22)",
    )
    assert normalize_metadata_search_query(title.local_title) == "OVERLORD"
    assert title.local_title == "OVERLORD (L15-L22)"
    assert title.relative_root_path == "Anime/OVERLORD (L15-L22)"


@pytest.mark.parametrize("title", ["(J19)", "J19", "A J19"])
def test_empty_or_too_short_result_falls_back_to_original(title):
    assert normalize_metadata_search_query(title) == title


def test_technical_part_uses_clean_collection_name_without_hierarchy_suffix():
    collection = CatalogCollection(
        local_title="Ansatsu Kyoushitsu (Z15-Z16)",
        normalized_local_title="ansatsu kyoushitsu z15 z16",
        relative_root_path="Anime/Ansatsu Kyoushitsu (Z15-Z16)",
    )
    title = CatalogTitle(
        local_title="Serie 2 (Z16)", normalized_local_title="serie 2 z16",
        relative_root_path="Anime/Ansatsu Kyoushitsu (Z15-Z16)/Serie 2 (Z16)",
        season_number=2, season_label="S2", collection=collection,
    )
    assert default_metadata_search_query(title) == "Ansatsu Kyoushitsu"


@pytest.mark.parametrize(("season_number", "season_label"), [(1, "S1"), (2, "S2")])
def test_manual_season_does_not_change_real_local_search_name(
    season_number, season_label,
):
    collection = CatalogCollection(
        local_title="OVERLORD (L15-L22)", normalized_local_title="overlord l15 l22",
        relative_root_path="Anime/OVERLORD (L15-L22)",
    )
    title = CatalogTitle(
        local_title="Overlord (L15)", normalized_local_title="overlord l15",
        relative_root_path="Anime/OVERLORD (L15-L22)/Overlord (L15)",
        part_type_manual="season", season_number_manual=season_number,
        season_label_manual=season_label, hierarchy_manual_override=True,
        collection=collection,
    )
    original_identity = (
        title.local_title, title.manual_display_title,
        title.part_type_manual, title.season_number_manual,
        title.season_label_manual, title.catalog_collection_id,
    )

    assert default_metadata_search_query(title) == "Overlord"
    assert (
        title.local_title, title.manual_display_title,
        title.part_type_manual, title.season_number_manual,
        title.season_label_manual, title.catalog_collection_id,
    ) == original_identity


def test_structural_season_uses_known_specific_metadata_title():
    collection = CatalogCollection(
        local_title="Peter Grill To Kenja No Jikan",
        normalized_local_title="peter grill to kenja no jikan",
        relative_root_path="Anime/Peter Grill To Kenja No Jikan",
    )
    title = CatalogTitle(
        local_title="season 2 P22", normalized_local_title="season 2 p22",
        relative_root_path="Anime/Peter Grill To Kenja No Jikan/season 2 P22",
        part_type="season", season_number=2, season_label="S2",
        collection=collection,
        metadata_record=TitleMetadata(
            display_title="Peter Grill To Kenja No Jikan - Super Extra",
            title_romaji="Peter Grill to Kenja no Jikan: Super Extra",
        ),
    )

    assert default_metadata_search_query(title) == (
        "Peter Grill to Kenja no Jikan: Super Extra"
    )
    assert title.local_title == "season 2 P22"
    assert collection.local_title == "Peter Grill To Kenja No Jikan"


def test_legitimate_season_words_in_real_title_are_preserved():
    title = CatalogTitle(
        local_title="The Anime Season 2 Story (P24)",
        normalized_local_title="the anime season 2 story p24",
        relative_root_path="Anime/The Anime Season 2 Story (P24)",
        part_type_manual="season", season_number_manual=2,
        season_label_manual="S2", hierarchy_manual_override=True,
    )

    assert default_metadata_search_query(title) == "The Anime Season 2 Story"
