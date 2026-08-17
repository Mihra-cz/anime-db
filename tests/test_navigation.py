import pytest

from app.main import (
    catalog_state_url,
    hardsub_return_url,
    metadata_return_url,
    safe_local_redirect_target,
    series_state_url,
)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/", "/"),
        ("/catalog/all?q=test", "/catalog/all?q=test"),
        ("/titles/123#metadata", "/titles/123#metadata"),
        ("https://evil.example", "/"),
        ("http://evil.example", "/"),
        ("//evil.example", "/"),
        ("https:/evil.example", "/"),
        (r"/\evil.example", "/"),
        (r"\\evil.example", "/"),
        ("///evil.example", "/"),
        ("catalog/all", "/"),
        ("", "/"),
        (None, "/"),
        ("/catalog/all\nLocation: https://evil.example", "/"),
    ],
)
def test_safe_local_redirect_target(target, expected):
    assert safe_local_redirect_target(target) == expected


def test_hardsub_return_url_preserves_filter_title_and_video_anchor():
    url = hardsub_return_url(
        "missing", "Anime/My Show", 42, "overlord", "title", "asc", "episode", "desc"
    )
    assert url == (
        "/catalog/missing/series?series_path=Anime%2FMy+Show&q=overlord&sort=title&"
        "direction=asc&video_sort=episode&video_direction=desc#video-42"
    )


def test_filter_search_and_sort_are_preserved_in_catalog_links():
    assert catalog_state_url("only-cs", "show", "title", "desc") == (
        "/catalog/only-cs?q=show&sort=title&direction=desc"
    )
    assert series_state_url("only-cs", "Anime/Show", "show", "title", "desc") == (
        "/catalog/only-cs/series?series_path=Anime%2FShow&q=show&sort=title&direction=desc"
    )


def test_metadata_return_preserves_catalog_and_detail_state():
    assert metadata_return_url(
        "missing", 7, "show", "title", "desc", "episode", "asc"
    ) == (
        "/titles/7?filter_name=missing&q=show&sort=title&direction=desc&"
        "video_sort=episode&video_direction=asc#metadata"
    )
