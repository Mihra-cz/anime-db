import pytest

from app.metadata.service import default_metadata_search_query, normalize_metadata_search_query
from app.models import CatalogCollection, CatalogTitle


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


def test_technical_part_uses_collection_name_and_season_for_default_query():
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
    assert default_metadata_search_query(title) == "Ansatsu Kyoushitsu Season 2"


def test_manual_season_uses_collection_name_for_default_query():
    collection = CatalogCollection(
        local_title="OVERLORD (L15-L22)", normalized_local_title="overlord l15 l22",
        relative_root_path="Anime/OVERLORD (L15-L22)",
    )
    title = CatalogTitle(
        local_title="Overlord (L15)", normalized_local_title="overlord l15",
        relative_root_path="Anime/OVERLORD (L15-L22)/Overlord (L15)",
        season_number_manual=1, season_label_manual="S1", collection=collection,
    )
    assert default_metadata_search_query(title) == "OVERLORD Season 1"
