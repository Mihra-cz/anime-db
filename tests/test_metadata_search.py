import pytest

from app.metadata.service import normalize_metadata_search_query
from app.models import CatalogTitle


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
