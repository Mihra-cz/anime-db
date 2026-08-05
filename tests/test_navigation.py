from app.main import catalog_state_url, hardsub_return_url, series_state_url


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
