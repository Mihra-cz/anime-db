from app.main import templates
from app.metadata.providers.base import ProviderTitleMetadata
from app.models import CatalogTitle


def test_detail_template_displays_anilist_candidates():
    candidate = ProviderTitleMetadata(
        provider="anilist", external_id="1", title_romaji="Test Show",
        title_english="The Test Show", title_native="テスト", release_year=2020,
        format="TV", episode_count=12, cover_image_url="https://img/1.jpg",
        site_url="https://anilist.co/anime/1",
    )
    title = CatalogTitle(
        id=1, local_title="Local Test", normalized_local_title="local test",
        relative_root_path="Anime/Local Test", metadata_status="unlinked",
    )
    rendered = templates.env.get_template("series.html").render(
        request=type("Request", (), {"url_for": lambda self, *args, **kwargs: "/static/style.css"})(),
        series=type("Series", (), {"name": "Local Test", "relative_path": "Anime/Local Test"})(),
        catalog_title=title, metadata_status_labels={"unlinked": "Bez metadat"},
        metadata_candidates=[candidate], metadata_error=None, filter_name="all",
        filter_label="Všechna videa", back_url="/catalog/all", videos=[], q="",
        sort="title", direction="asc", video_sort="default", video_direction="asc",
        video_sort_url=lambda _: "#", translation_status=lambda _: None,
        video_matches_filter=lambda *_: False, derive_season_info=lambda _: None,
        derive_episode_number=lambda _: None,
    )
    assert "The Test Show" in rendered
    assert "https://img/1.jpg" in rendered
    assert "Otevřít na AniListu" in rendered
    assert 'name="metadata_query"' in rendered
