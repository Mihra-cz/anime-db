from app.config import Settings
from app.database import Base
from app.main import app, create_app, templates
from app.hierarchy_review import simple_definition_rows, single_season_suggestion
from app.metadata.providers.base import ProviderTitleMetadata
from app.models import (
    CatalogCollection, CatalogTitle, ExternalSubtitle, InternalSubtitle, TitleMetadata, Video,
    utc_now,
)
from app.numbering import summarize_title_numbering
from starlette.requests import Request


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


def test_episode_table_prioritizes_readable_data_and_preserves_editing_and_values():
    title = CatalogTitle(
        id=1, local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", metadata_status="unlinked",
        season_number=1, season_label="S1", numbering_mode="season_local",
        episode_start_offset=3, manual_display_title="Manual Show",
        metadata_record=TitleMetadata(display_title="Metadata Show"),
    )
    video = Video(
        id=1, relative_path="Anime/Show/Season 1/Title - 01.mkv",
        root_folder="Anime", filename="Title - 01.mkv", size=1, mtime_ns=1,
        duration=1450,
        local_episode_number=1, season_episode_number=1,
        absolute_episode_number=4, external_episode_number=1,
        episode_number_source="manual", episode_number_manual_override=1,
        episode_number_confidence=1.0, episode_number_verified_at=utc_now(),
        manual_hardsub_cs=True, manual_hardsub_verified_at=utc_now(),
        internal_subtitles=[InternalSubtitle(
            stream_index=2, codec="ass", language="cze", normalized_language="cs",
        )],
        external_subtitles=[ExternalSubtitle(
            relative_path="Anime/Show/Season 1/Title - 01.en.srt",
            codec="srt", language="eng", normalized_language="eng",
        )],
    )
    status = type("Status", (), {
        "automatic_has_cs": False, "automatic_has_sk": False, "has_unknown": False,
    })()
    season = type("Season", (), {"original": "Season 1"})()

    values_before_render = (
        video.local_episode_number, video.season_episode_number,
        video.absolute_episode_number, video.external_episode_number,
        video.catalog_title_id, title.season_number, title.season_label,
    )
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

    assert "<th><a class=\"sort-link\" href=\"#\">Název" in rendered
    assert '<strong>Manual Show</strong><small class="technical-filename">Title - 01.mkv</small>' in rendered
    assert ">S1<" in rendered
    assert "<strong>E1</strong>" in rendered
    assert "<small>A4</small>" in rendered
    assert '<small class="technical-meta">L1 · X1</small>' in rendered
    assert "S1 A4 E1" not in rendered
    assert ">24:10<" in rendered
    assert "CZ (ASS)" in rendered
    assert "EN (SRT)" in rendered
    assert rendered.count("<th>Titulky</th>") == 1
    assert ">Ano<small>CZ</small>" in rendered
    assert "Hardsub potvrzen" in rendered
    assert "Číslování ručně ověřeno" in rendered
    assert 'action="/videos/1/episode-number"' in rendered
    assert 'action="/videos/1/hardsub"' in rendered
    assert (
        video.local_episode_number, video.season_episode_number,
        video.absolute_episode_number, video.external_episode_number,
        video.catalog_title_id, title.season_number, title.season_label,
    ) == values_before_render


def test_episode_table_uses_metadata_title_and_safe_empty_subtitle_fallback():
    title = CatalogTitle(
        id=2, local_title="Local Show", normalized_local_title="local show",
        relative_root_path="Anime/Local Show", metadata_status="linked_manual",
        part_type="ova", metadata_record=TitleMetadata(display_title="Metadata Show"),
    )
    video = Video(
        id=2, relative_path="Anime/Local Show/OVA.mkv", root_folder="Anime",
        filename="OVA.mkv", size=1, mtime_ns=1, catalog_title=title,
        manual_hardsub_verified_at=utc_now(),
    )
    unknown_video = Video(
        id=3, relative_path="Anime/Local Show/Unknown.mkv", root_folder="Anime",
        filename="Unknown.mkv", size=1, mtime_ns=1, catalog_title=title,
    )
    status = type("Status", (), {
        "automatic_has_cs": False, "automatic_has_sk": False, "has_unknown": False,
    })()
    rendered = templates.env.get_template("series.html").render(
        request=type("Request", (), {"url_for": lambda self, *args, **kwargs: "/static/style.css"})(),
        series=type("Series", (), {"name": "Local Show", "relative_path": "Anime/Local Show"})(),
        catalog_title=title, metadata_status_labels={"linked_manual": "Spárováno ručně"},
        metadata_candidates=[], metadata_error=None, metadata_message=None,
        metadata_warning=None, numbering_error=None, numbering_message=None,
        numbering_preview=None, sequence_start=None, metadata_allow_remote_images=False,
        metadata_default_query="Local Show", filter_name="all", filter_label="Všechna videa",
        back_url="/catalog/all", videos=[video, unknown_video], q="", sort="title", direction="asc",
        video_sort="default", video_direction="asc", video_sort_url=lambda _: "#",
        translation_status=lambda _: status, video_matches_filter=lambda *_: False,
        derive_season_info=lambda _: type("Season", (), {"original": None})(),
        derive_episode_number=lambda _: None,
    )

    assert '<strong>Metadata Show</strong><small class="technical-filename">OVA.mkv</small>' in rendered
    assert ">OVA<" in rendered
    assert '<td class="subtitle-list">—</td>' in rendered
    assert '<td class="compact-column">Ne</td>' in rendered
    assert '<td class="compact-column">Neznámé</td>' in rendered
    assert "Hardsub nepřítomen" in rendered
    assert '<td class="verification-column">Neověřeno</td>' in rendered


def test_existing_manual_video_edits_still_persist_without_changing_hierarchy(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", hierarchy_status="verified",
            hierarchy_verified_at=utc_now(),
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1", part_type="season",
            season_number=1, season_label="S1", numbering_mode="season_local",
            hierarchy_verified_at=utc_now(),
        )
        video = Video(
            relative_path="Anime/Show/Season 1/01.mkv", root_folder="Anime",
            filename="01.mkv", size=1, mtime_ns=1, file_type="episode",
            local_episode_number=1, catalog_title=title,
            catalog_collection=collection,
        )
        session.add(video)
        session.commit()
        video_id, title_id = video.id, title.id
        original_hierarchy = (
            title.season_number, title.season_label,
            title.hierarchy_verified_at.replace(tzinfo=None),
        )

    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }
    response = endpoints["/videos/{video_id}/episode-number"](
        video_id, manual_episode_number="7", filter_name="all", q="", sort="",
        direction="", detail_sort="", detail_direction="",
    )
    assert response.status_code == 303
    response = endpoints["/videos/{video_id}/hardsub"](
        video_id, mode="cs", filter_name="all", series_path="",
        catalog_title_id=title_id, q="", sort="", direction="", video_sort="",
        video_direction="",
    )
    assert response.status_code == 303

    with web_app.state.sessions() as session:
        stored_video = session.get(Video, video_id)
        stored_title = session.get(CatalogTitle, title_id)
        assert stored_video.episode_number_manual_override == 7
        assert stored_video.season_episode_number == 7
        assert stored_video.manual_hardsub_cs is True
        assert stored_video.manual_hardsub_verified_at is not None
        assert (
            stored_title.season_number,
            stored_title.season_label,
            stored_title.hierarchy_verified_at,
        ) == original_hierarchy


def test_root_folder_link_has_readable_label_and_no_dead_dot_url():
    stats = {
        "episodes": 0, "bonus": 2, "cs": 0, "sk": 0, "translated": 0,
        "missing": 2, "unknown": 0,
    }
    request = type("Request", (), {
        "url_for": lambda self, name, **kwargs: (
            "/root-videos" if name == "root_videos" else "/static/style.css"
        ),
    })()

    rendered = templates.env.get_template("index.html").render(
        request=request, folders=[(".", stats)], totals={
            "episodes": 0, "bonus": 2, "only_cs": 0, "only_sk": 0,
            "both_cs_sk": 0, "missing": 2, "unknown": 0,
        }, message=None, error=None, confirm_deletions=False, q="",
    )

    assert 'href="/root-videos">Nezařazená videa z kořene knihovny</a>' in rendered
    assert "/folders/." not in rendered
    assert ">.</a>" not in rendered
    assert ">2</td>" in rendered

    group = type("Group", (), {
        "is_root_group": True, "name": "Nezařazená videa z kořene knihovny",
        "total": 2, "episodes": 0, "bonus": 2, "cs": 0, "sk": 0,
        "missing": 2, "unknown": 0, "translated": 0, "matched": 2,
        "parts": 1, "linked_parts": 0, "relative_path": ".",
        "catalog_collection_id": None,
    })()
    catalog_rendered = templates.env.get_template("catalog.html").render(
        request=request, filter_label="Všechna videa", filter_name="all",
        groups=[group], video_count=2, q="", sort="matched", direction="desc",
        all_filters={"all": "Všechna videa"}, sort_url=lambda _: "#",
        catalog_state_url=lambda *_: "#",
    )
    assert 'href="/root-videos"' in catalog_rendered
    assert "/collections/None" not in catalog_rendered


def test_root_video_page_lists_files_and_manual_assignment_keeps_physical_paths(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'root-web.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        target_collection = CatalogCollection(
            local_title="Existing Movie", normalized_local_title="existing movie",
            relative_root_path="Anime/Existing Movie",
        )
        target_title = CatalogTitle(
            collection=target_collection, local_title="Existing Movie",
            normalized_local_title="existing movie",
            relative_root_path="Anime/Existing Movie/title", part_type="film",
        )
        first = Video(
            relative_path="First Movie.mkv", root_folder=".", filename="First Movie.mkv",
            size=1, mtime_ns=1, file_type="other",
        )
        second = Video(
            relative_path="Second Movie.mkv", root_folder=".", filename="Second Movie.mkv",
            size=1, mtime_ns=1, file_type="other",
        )
        session.add_all([target_title, first, second])
        session.commit()
        target_title_id, first_id, second_id = target_title.id, first.id, second.id

    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }
    request = Request({
        "type": "http", "app": web_app, "method": "GET", "path": "/root-videos",
        "root_path": "", "scheme": "http", "query_string": b"", "headers": [],
        "server": ("testserver", 80), "client": ("testclient", 50000),
    })
    response = endpoints["/root-videos"](request)
    rendered = response.body.decode()

    assert "First Movie.mkv" in rendered
    assert "Second Movie.mkv" in rendered
    assert "Společné umístění z nich nedělá jednu anime kolekci" in rendered
    assert f'action="/root-videos/{first_id}/assignment"' in rendered
    assert f'action="/root-videos/{second_id}/new-title"' in rendered
    assert "Existing Movie" in rendered
    assert rendered.count("Neznámé") >= 2

    assignment_response = endpoints["/root-videos/{video_id}/assignment"](
        first_id, target_title_id=str(target_title_id)
    )
    creation_response = endpoints["/root-videos/{video_id}/new-title"](
        second_id, display_title="Second Movie", part_type="film"
    )
    assert assignment_response.status_code == creation_response.status_code == 303

    with web_app.state.sessions() as session:
        stored_first = session.get(Video, first_id)
        stored_second = session.get(Video, second_id)
        assert stored_first.relative_path == "First Movie.mkv"
        assert stored_second.relative_path == "Second Movie.mkv"
        assert stored_first.catalog_title_id == target_title_id
        assert stored_second.catalog_title.local_title == "Second Movie"
        assert stored_first.catalog_collection_id != stored_second.catalog_collection_id
