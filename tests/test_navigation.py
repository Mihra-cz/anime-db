from app.main import hardsub_return_url


def test_hardsub_return_url_preserves_filter_title_and_video_anchor():
    url = hardsub_return_url("missing", "Anime/My Show", 42)
    assert url == "/catalog/missing/series?series_path=Anime%2FMy+Show#video-42"
