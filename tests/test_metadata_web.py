from app.main import app, templates
from app.hierarchy_review import simple_definition_rows, single_season_suggestion
from app.metadata.providers.base import ProviderTitleMetadata
from app.models import CatalogCollection, CatalogTitle, Video, utc_now
from app.numbering import summarize_title_numbering


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
        metadata_allow_remote_images=True,
        metadata_default_query="Local Test",
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
    assert "Použít tato metadata" in rendered


def test_remote_candidate_image_can_be_disabled():
    candidate = ProviderTitleMetadata(
        provider="anilist", external_id="1", title_romaji="Test",
        cover_image_url="https://img/secret.jpg",
    )
    title = CatalogTitle(
        id=1, local_title="Local", normalized_local_title="local",
        relative_root_path="Anime/Local", metadata_status="unlinked",
    )
    rendered = templates.env.get_template("series.html").render(
        request=type("Request", (), {"url_for": lambda self, *args, **kwargs: "/static/style.css"})(),
        series=type("Series", (), {"name": "Local", "relative_path": "Anime/Local"})(),
        catalog_title=title, metadata_status_labels={"unlinked": "Bez metadat"},
        metadata_candidates=[candidate], metadata_error=None, metadata_message=None,
        metadata_allow_remote_images=False, filter_name="all", filter_label="Všechna videa",
        metadata_default_query="Local",
        back_url="/catalog/all", videos=[], q="", sort="title", direction="asc",
        video_sort="default", video_direction="asc", video_sort_url=lambda _: "#",
        translation_status=lambda _: None, video_matches_filter=lambda *_: False,
        derive_season_info=lambda _: None, derive_episode_number=lambda _: None,
    )
    assert "https://img/secret.jpg" not in rendered
    assert "Test" in rendered


def test_manual_metadata_query_is_not_normalized_again():
    source = templates.env.get_template("series.html").render(
        request=type("Request", (), {"url_for": lambda self, *args, **kwargs: "/static/style.css"})(),
        series=type("Series", (), {"name": "Local", "relative_path": "Anime/Local"})(),
        catalog_title=CatalogTitle(
            id=1, local_title="Local (J19)", normalized_local_title="local j19",
            relative_root_path="Anime/Local (J19)", metadata_status="unlinked",
        ),
        metadata_status_labels={"unlinked": "Bez metadat"}, metadata_candidates=[],
        metadata_error=None, metadata_message=None, metadata_allow_remote_images=True,
        metadata_default_query="Local", metadata_query="My exact query (J19)",
        filter_name="all", filter_label="Všechna videa", back_url="/catalog/all",
        videos=[], q="", sort="title", direction="asc", video_sort="default",
        video_direction="asc", video_sort_url=lambda _: "#",
        translation_status=lambda _: None, video_matches_filter=lambda *_: False,
        derive_season_info=lambda _: None, derive_episode_number=lambda _: None,
    )
    assert 'value="My exact query (J19)"' in source


def test_all_metadata_mutations_are_post_only():
    mutation_suffixes = {
        "/metadata/confirm", "/metadata/update", "/metadata/unlink",
        "/metadata/lock", "/display-title",
    }
    matching = [route for route in app.routes if any(route.path.endswith(s) for s in mutation_suffixes)]
    assert len(matching) == len(mutation_suffixes)
    assert all(route.methods == {"POST"} for route in matching)


def test_candidate_and_artwork_mutations_are_post_only():
    paths = {route.path: route.methods for route in app.routes if hasattr(route, "methods")}
    assert paths["/catalog/{filter_name}/titles/{catalog_title_id}/metadata/candidates/{candidate_id}/reject"] == {"POST"}
    assert paths["/catalog/{filter_name}/titles/{catalog_title_id}/metadata/artwork/refresh"] == {"POST"}
    assert paths["/metadata/batch-search"] == {"POST"}
    assert paths["/titles/{catalog_title_id}/numbering/sequence"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/season-one"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/simple-preview"] == {"POST"}


def _render_hierarchy_review(*, verified=False):
    collection = CatalogCollection(
        id=1, local_title="Akame ga Kill! (L14)",
        normalized_local_title="akame ga kill l14",
        relative_root_path="Anime/Akame ga Kill! (L14)",
        hierarchy_status="verified" if verified else "review_required",
        hierarchy_verified_at=utc_now() if verified else None,
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title=collection.local_title,
        normalized_local_title=collection.normalized_local_title,
        relative_root_path=collection.relative_root_path, part_type="title",
        hierarchy_verified_at=utc_now() if verified else None,
    )
    video = Video(
        id=1, relative_path=f"{collection.relative_root_path}/Episode 01.mkv",
        root_folder="Anime", filename="Episode 01.mkv", size=1, mtime_ns=1,
        season_episode_number=1, catalog_title=title,
        catalog_collection=collection,
    )
    summary = summarize_title_numbering([video])
    return templates.env.get_template("hierarchy_review_detail.html").render(
        request=type("Request", (), {
            "url_for": lambda self, *args, **kwargs: "/static/style.css",
        })(),
        collection=collection, videos=[video], numbering_unknown=0,
        message=None, error=None, season_one_suggestion=single_season_suggestion(collection),
        title_numbering=[{"title": title, "summary": summary}],
        metadata_status_labels={"unlinked": "Bez metadat"},
        simple_rows=simple_definition_rows(collection), definitions_json="[]",
        external_search_candidates=[], external_candidates=[],
        preview=None, preview_rows=[],
    )


def test_hierarchy_review_shows_season_one_suggestion_and_human_friendly_form():
    rendered = _render_hierarchy_review()

    assert "Pravděpodobně jednoduchá jednosériová kolekce" in rendered
    assert "Nastavit jako Season 1" in rendered
    assert "Jednoduchá definice ručního rozdělení" in rendered
    assert "Název části" in rendered
    assert "Číslo sezóny" in rendered
    assert "Rozsah epizod od" in rendered
    assert "Virtuální rozdělení nemění ani nepřesouvá fyzické soubory na NASu." in rendered
    assert "<summary>Pokročilé / zobrazit JSON</summary>" in rendered
    assert 'name="definitions_json"' in rendered
    assert rendered.index("Jednoduchá definice ručního rozdělení") < rendered.index(
        "Pokročilé / zobrazit JSON"
    )


def test_collection_and_title_verification_texts_are_distinct():
    rendered = _render_hierarchy_review(verified=True)

    assert "Hierarchie ověřena" in rendered
    assert "Zařazení ověřeno" in rendered
    assert "Nastavit jako Season 1" not in rendered


def test_episode_column_distinguishes_season_episode_absolute_and_external_numbers():
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", metadata_status="unlinked",
        season_number=1, season_label="S1", numbering_mode="season_local",
        episode_start_offset=3,
    )
    video = Video(
        id=1, relative_path="Anime/Show/Season 1/Title - 01.mkv",
        root_folder="Anime", filename="Title - 01.mkv", size=1, mtime_ns=1,
        local_episode_number=1, season_episode_number=1,
        absolute_episode_number=4, external_episode_number=1,
        episode_number_source="manual", episode_number_manual_override=1,
    )
    status = type("Status", (), {
        "automatic_has_cs": False, "automatic_has_sk": False, "has_unknown": False,
    })()
    season = type("Season", (), {"original": "Season 1"})()

    rendered = templates.env.get_template("series.html").render(
        request=type("Request", (), {"url_for": lambda self, *args, **kwargs: "/static/style.css"})(),
        series=type("Series", (), {"name": "Show", "relative_path": "Anime/Show"})(),
        catalog_title=title, metadata_status_labels={"unlinked": "Bez metadat"},
        metadata_candidates=[], metadata_error=None, metadata_message=None,
        metadata_warning=None, numbering_error=None, numbering_message=None,
        numbering_preview=None,
        sequence_start=None, metadata_allow_remote_images=False,
        metadata_default_query="Show", filter_name="all",
        filter_label="Všechna videa", back_url="/catalog/all", videos=[video], q="",
        sort="title", direction="asc", video_sort="default", video_direction="asc",
        video_sort_url=lambda _: "#", translation_status=lambda _: status,
        video_matches_filter=lambda *_: False, derive_season_info=lambda _: season,
        derive_episode_number=lambda _: 1,
    )

    assert ">S1<" in rendered
    assert "<strong>E1</strong>" in rendered
    assert "A4" in rendered
    assert "lokální L1" in rendered
    assert "externí X1" in rendered
    assert "S1 A4 E1" not in rendered
