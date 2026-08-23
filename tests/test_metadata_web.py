import asyncio
from http.cookies import SimpleCookie
from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import HTTPException
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.catalog import (
    ROOT_VIDEO_GROUP_LABEL,
    TITLE_NAME_PREFERENCE_LABELS,
    build_catalog_results,
    detect_episode_number,
    effective_video_content_display,
    is_film_video,
    video_matches_filter,
)
from app.config import Settings
from app.database import Base
from app.main import (
    PREFERRED_TITLE_LANGUAGE_COOKIE, app, create_app,
    get_preferred_title_language, templates,
)
from app.hierarchy_review import (
    CONFIRMED_DUPLICATES_REVIEW_REASON, PERIOD_HINT_REVIEW_REASON,
    confirm_duplicate_videos, separate_nonstandard_videos,
    refresh_collection_state, simple_definition_rows, single_title_confirmation_suggestion,
    supplementary_assignment_recommendations,
)
from app.hierarchy_types import PART_TYPE_CHOICES, VIDEO_CONTENT_TYPE_CHOICES
from app.metadata.providers.base import ProviderTitleMetadata
from app.migrations import migrate_schema
from app.models import (
    CatalogCollection, CatalogTitle, ExternalSubtitle, ExternalTitleLink,
    InternalSubtitle, TitleMetadata, Video, utc_now,
)
from app.numbering import (
    recalculate_title_numbering, summarize_title_numbering,
    unresolved_duplicate_groups,
)
from app.scanner import scan_library
from starlette.requests import Request


class RecordingMetadataProvider:
    def __init__(self, results):
        self.results = list(results)
        self.search_calls = []
        self.fetch_calls = []

    def search_titles(self, query):
        self.search_calls.append(query)
        return list(self.results)

    def fetch_title(self, external_id):
        self.fetch_calls.append(str(external_id))
        return next(
            item for item in self.results if item.external_id == str(external_id)
        )


def metadata_candidate(
    external_id, romaji, english, native, *, episode_count=12,
):
    return ProviderTitleMetadata(
        provider="anilist", external_id=str(external_id), title_romaji=romaji,
        title_english=english, title_native=native, release_year=2020,
        format="TV", episode_count=episode_count,
        site_url=f"https://anilist.co/anime/{external_id}",
    )


def web_request(web_app, path, *, cookie=None):
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request({
        "type": "http", "app": web_app, "method": "GET", "path": path,
        "root_path": "", "scheme": "http", "query_string": b"",
        "headers": headers, "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })


def post_form_request(web_app, path, items):
    body = urlencode(items).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "app": web_app, "method": "POST", "path": path,
        "root_path": "", "scheme": "http", "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "server": ("testserver", 80), "client": ("testclient", 50000),
    }, receive)


def select_option_values(rendered: str, name: str) -> list[tuple[str, ...]]:
    blocks = re.findall(
        rf'<select name="{re.escape(name)}"[^>]*>(.*?)</select>', rendered, re.DOTALL,
    )
    return [tuple(re.findall(r'<option value="([^"]*)"', block)) for block in blocks]


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
        show_metadata_candidates=True, candidate_reasons={}, low_score_threshold=0.55,
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
        show_metadata_candidates=True, candidate_reasons={}, low_score_threshold=0.55,
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
    assert paths["/videos/{video_id}/media-part"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/confirm-part"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/simple-preview"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/separate-nonstandard"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/manage-videos"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/duplicates/confirm"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/duplicates/confirm-bulk"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/duplicates/clear"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/merge-title"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/delete-empty-title"] == {"POST"}
    assert paths["/titles/{catalog_title_id}/delete-empty"] == {"POST"}
    assert paths["/hierarchy-review/collections/delete-empty-bulk"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/numbering-preview"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/numbering-apply"] == {"POST"}
    assert paths["/preferences/title-name"] == {"POST"}


def test_empty_title_detail_shows_metadata_warning_and_delete_action(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'empty-title-detail.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="High School DxD", normalized_local_title="high school dxd",
            relative_root_path="Anime/High School DxD",
        )
        title = CatalogTitle(
            collection=collection, local_title="High School DxD OVA",
            normalized_local_title="high school dxd ova",
            relative_root_path="Anime/High School DxD/OVA",
            metadata_status="linked_manual",
            metadata_record=TitleMetadata(
                display_title="High School DxD OVA", metadata_provider="anilist",
                metadata_external_id="1",
            ),
        )
        title.external_links.append(ExternalTitleLink(
            provider="anilist", external_id="1", match_method="manual_search",
            is_primary=True, is_manual=True,
        ))
        session.add(collection)
        session.commit()
        title_id, collection_id = title.id, collection.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/titles/{catalog_title_id}"
    )
    rendered = endpoint(
        web_request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()

    assert "Odstranit prázdnou část" in rendered
    assert f'action="/titles/{title_id}/delete-empty"' in rendered
    assert "Současně bude odstraněno 2 vlastněných metadata/reference záznamů" in rendered
    assert "collection ani NAS se nemění" in rendered

    delete_endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/titles/{catalog_title_id}/delete-empty"
    )
    response = delete_endpoint(title_id, confirm_delete=True)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/hierarchy-review?message=")
    with web_app.state.sessions() as session:
        assert session.get(CatalogTitle, title_id) is None
        assert session.get(CatalogCollection, collection_id) is not None
        assert session.get(CatalogCollection, collection_id).titles == []


def test_hierarchy_review_distinguishes_manual_split_empty_title_delete(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'manual-empty-title-ui.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="High School DxD", normalized_local_title="high school dxd",
            relative_root_path="Anime/High School DxD",
        )
        manual = CatalogTitle(
            collection=collection, local_title="NC – High School DxD New",
            normalized_local_title="nc high school dxd new",
            relative_root_path="Anime/High School DxD/.catalog-part-1",
            hierarchy_manual_override=True, part_type_manual="bonus",
        )
        automatic = CatalogTitle(
            collection=collection, local_title="Unused automatic",
            normalized_local_title="unused automatic",
            relative_root_path="Anime/High School DxD/Unused",
        )
        session.add(collection)
        session.commit()
        collection_id, manual_id, automatic_id = (
            collection.id, manual.id, automatic.id,
        )

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )
    rendered = endpoint(
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()
    manual_block = rendered.split(
        f'id="title-{manual_id}"', 1
    )[1].split("</article>", 1)[0]
    automatic_block = rendered.split(
        f'id="title-{automatic_id}"', 1
    )[1].split("</article>", 1)[0]

    assert "Tato prázdná část je součástí ruční definice rozdělení" in manual_block
    assert 'name="remove_from_manual_split" value="true"' in manual_block
    assert "Odstranit část i z ručního rozdělení" in manual_block
    assert "Potvrzuji odstranění části i konkrétní položky" in manual_block
    assert "Odstranit prázdnou část" in automatic_block
    assert "remove_from_manual_split" not in automatic_block
    assert "NC – High School DxD New" in rendered
    assert "Jednoduchá definice ručního rozdělení" in rendered

    delete_endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None)
        == "/hierarchy-review/{collection_id}/delete-empty-title"
    )
    response = delete_endpoint(
        collection_id, title_id=manual_id, confirm_delete=True,
        remove_from_manual_split=True,
    )
    assert response.status_code == 303
    assert "ru%C4%8Dn%C3%ADho+rozd%C4%9Blen%C3%AD" in response.headers["location"]
    with web_app.state.sessions() as session:
        assert session.get(CatalogTitle, manual_id) is None
        assert session.get(CatalogTitle, automatic_id) is not None


def test_bulk_empty_collection_endpoint_reports_deleted_and_skipped(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'bulk-empty.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collections = [
            CatalogCollection(
                local_title=name, normalized_local_title=name.casefold(),
                relative_root_path=f"@manual/{name.casefold().replace(' ', '-')}",
            )
            for name in ("Empty A", "Empty B", "Changed")
        ]
        session.add_all(collections)
        session.commit()
        empty_a_id, empty_b_id, changed_id = [item.id for item in collections]

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    overview = endpoints["/hierarchy-review"](
        web_request(web_app, "/hierarchy-review")
    ).body.decode()
    assert "Vybrat všechny" in overview
    assert "Zrušit výběr" in overview
    assert "Odstranit vybrané prázdné collections" in overview
    assert overview.count("Potvrzuji odstranění prázdné DB collection") == 3

    # Simuluje změnu stavu mezi vykreslením formuláře a odesláním requestu.
    with web_app.state.sessions() as session:
        changed = session.get(CatalogCollection, changed_id)
        session.add(CatalogTitle(
            collection=changed, local_title="New part", normalized_local_title="new part",
            relative_root_path="@manual/changed/new-part",
        ))
        session.commit()

    body = urlencode([
        ("collection_ids", str(empty_a_id)),
        ("collection_ids", str(empty_b_id)),
        ("collection_ids", str(changed_id)),
        ("confirm_delete", "true"),
    ]).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http", "app": web_app, "method": "POST",
        "path": "/hierarchy-review/collections/delete-empty-bulk",
        "root_path": "", "scheme": "http", "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "server": ("testserver", 80), "client": ("testclient", 50000),
    }, receive)
    response = asyncio.run(
        endpoints["/hierarchy-review/collections/delete-empty-bulk"](request)
    )

    assert response.status_code == 303
    location = response.headers["location"]
    message = parse_qs(urlparse(location).query)["message"][0]
    assert "Odstraněné collections: Empty A, Empty B" in message
    assert "Přeskočené: Changed" in message
    with web_app.state.sessions() as session:
        assert session.get(CatalogCollection, empty_a_id) is None
        assert session.get(CatalogCollection, empty_b_id) is None
        assert session.get(CatalogCollection, changed_id) is not None


def test_first_metadata_search_redirects_to_visible_persisted_candidates(tmp_path):
    settings = Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'metadata-search.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    )
    web_app = create_app(settings)
    provider = RecordingMetadataProvider([
        metadata_candidate("101", "First Romaji", "First English", "第一"),
        metadata_candidate("102", "Second Romaji", "Second English", "第二"),
    ])
    web_app.state.metadata_provider = provider
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        title = CatalogTitle(
            local_title="Serie 1", normalized_local_title="serie 1",
            relative_root_path="Anime/Example/Serie 1",
        )
        video = Video(
            relative_path="Anime/Example/Serie 1/Example 01.mkv",
            root_folder="Anime", filename="Example 01.mkv", size=1, mtime_ns=1,
            file_type="episode", local_episode_number=1,
            season_episode_number=1, catalog_title=title,
        )
        session.add(video)
        session.commit()
        title_id = title.id

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    response = endpoints[
        "/catalog/{filter_name}/titles/{catalog_title_id}/metadata/search"
    ](
        "all", title_id, metadata_query="Example", q="", sort="",
        direction="", video_sort="", video_direction="",
    )

    assert response.status_code == 303
    parsed = urlparse(response.headers["location"])
    query = parse_qs(parsed.query)
    assert parsed.path == f"/titles/{title_id}"
    assert query["show_metadata_candidates"] == ["true"]
    assert query["metadata_query"] == ["Example"]
    assert provider.search_calls == ["Example"]

    rendered = endpoints["/titles/{catalog_title_id}"](
        web_request(web_app, parsed.path), title_id,
        show_metadata_candidates=True, metadata_query="Example",
        message=query["message"][0],
    ).body.decode()

    assert "Výběr externích metadat" in rendered
    assert "First Romaji" in rendered
    assert "Second Romaji" in rendered
    assert "nalezeno 2 kandidátů" in rendered
    # Navazující GET pouze načetl uložené výsledky a druhý search nebyl potřeba.
    assert provider.search_calls == ["Example"]


def test_metadata_change_uses_stored_candidates_and_preserves_local_hierarchy(tmp_path):
    settings = Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'metadata-change.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    )
    web_app = create_app(settings)
    first = metadata_candidate(
        "201", "Original Romaji", "Original English", "原作", episode_count=24,
    )
    second = metadata_candidate(
        "202", "Changed Romaji", "Changed English", "変更", episode_count=13,
    )
    provider = RecordingMetadataProvider([first, second])
    web_app.state.metadata_provider = provider
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Example", normalized_local_title="example",
            relative_root_path="Anime/Example", hierarchy_status="verified",
            hierarchy_verified_at=utc_now(),
        )
        title = CatalogTitle(
            collection=collection, local_title="Serie 1",
            normalized_local_title="serie 1",
            relative_root_path="Anime/Example/Serie 1", part_type="season",
            season_number=1, season_label="S1", numbering_mode="season_local",
            hierarchy_verified_at=utc_now(),
        )
        video = Video(
            relative_path="Anime/Example/Serie 1/Example 01.mkv",
            root_folder="Anime", filename="Example 01.mkv", size=1, mtime_ns=1,
            file_type="episode", local_episode_number=1,
            season_episode_number=1, absolute_episode_number=1,
            external_episode_number=None, episode_number_source="filename",
            episode_number_confidence=0.95, catalog_title=title,
            catalog_collection=collection,
        )
        session.add(video)
        session.commit()
        title_id, video_id = title.id, video.id
        unchanged_title = (
            title.local_title, title.manual_display_title, title.part_type,
            title.season_number, title.season_label, title.numbering_mode,
        )
        unchanged_video = (
            video.catalog_title_id, video.catalog_collection_id, video.filename,
            video.relative_path, video.root_folder, video.local_episode_number,
            video.season_episode_number, video.absolute_episode_number,
            video.external_episode_number, video.episode_number_source,
            video.episode_number_confidence,
        )

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    search = endpoints[
        "/catalog/{filter_name}/titles/{catalog_title_id}/metadata/search"
    ]
    confirm = endpoints[
        "/catalog/{filter_name}/titles/{catalog_title_id}/metadata/confirm"
    ]
    detail = endpoints["/titles/{catalog_title_id}"]

    search(
        "all", title_id, metadata_query="Example", q="", sort="",
        direction="", video_sort="", video_direction="",
    )
    with web_app.state.sessions() as session:
        candidates = {
            item.external_id: item.id
            for item in session.get(CatalogTitle, title_id).metadata_candidates
        }
    confirm(
        "all", title_id, external_id="201", candidate_id=candidates["201"],
        confirm_conflict=False, confirm_locked=False, q="", sort="",
        direction="", detail_sort="", detail_direction="",
    )

    normal = detail(
        web_request(web_app, f"/titles/{title_id}"), title_id
    ).body.decode()
    assert "Original Romaji" in normal
    assert "Aktuální vazba: <strong>anilist</strong> · ID 201" in normal
    assert "Změnit metadata" in normal
    assert "Vyhledat metadata znovu" in normal
    assert "Changed Romaji" not in normal
    assert "Výběr externích metadat" not in normal

    search_calls_before_change_view = list(provider.search_calls)
    change_view = detail(
        web_request(web_app, f"/titles/{title_id}"), title_id,
        show_metadata_candidates=True,
    ).body.decode()
    assert "Výběr externích metadat" in change_view
    assert "Changed Romaji" in change_view
    assert "Aktuálně přiřazeno" in change_view
    assert provider.search_calls == search_calls_before_change_view

    provider.results.append(metadata_candidate(
        "203", "Fresh Romaji", "Fresh English", "新規"
    ))
    refreshed = search(
        "all", title_id, metadata_query="Example fresh", q="", sort="",
        direction="", video_sort="", video_direction="",
    )
    assert refreshed.status_code == 303
    assert provider.search_calls == ["Example", "Example fresh"]
    refreshed_page = detail(
        web_request(web_app, f"/titles/{title_id}"), title_id,
        show_metadata_candidates=True, metadata_query="Example fresh",
    ).body.decode()
    assert "Fresh Romaji" in refreshed_page

    confirm(
        "all", title_id, external_id="202", candidate_id=candidates["202"],
        confirm_conflict=False, confirm_locked=False, q="", sort="",
        direction="", detail_sort="", detail_direction="",
    )
    for preference, expected in (
        ("romaji", "Changed Romaji"),
        ("english", "Changed English"),
        ("native", "変更"),
    ):
        rendered = detail(
            web_request(
                web_app, f"/titles/{title_id}",
                cookie=f"{PREFERRED_TITLE_LANGUAGE_COOKIE}={preference}",
            ),
            title_id,
        ).body.decode()
        assert f"<h1>{expected}</h1>" in rendered

    with web_app.state.sessions() as session:
        stored_title = session.get(CatalogTitle, title_id)
        stored_video = session.get(Video, video_id)
        assert (
            stored_title.local_title, stored_title.manual_display_title,
            stored_title.part_type, stored_title.season_number,
            stored_title.season_label, stored_title.numbering_mode,
        ) == unchanged_title
        assert (
            stored_video.catalog_title_id, stored_video.catalog_collection_id,
            stored_video.filename, stored_video.relative_path, stored_video.root_folder,
            stored_video.local_episode_number, stored_video.season_episode_number,
            stored_video.absolute_episode_number, stored_video.external_episode_number,
            stored_video.episode_number_source, stored_video.episode_number_confidence,
        ) == unchanged_video
        primary = session.scalar(select(ExternalTitleLink).where(
            ExternalTitleLink.catalog_title_id == title_id,
            ExternalTitleLink.is_primary.is_(True),
        ))
        assert primary.external_id == "202"
        assert stored_title.metadata_record.metadata_external_id == "202"

    restarted_app = create_app(settings)
    with restarted_app.state.sessions() as session:
        persisted = session.scalar(select(ExternalTitleLink).where(
            ExternalTitleLink.catalog_title_id == title_id,
            ExternalTitleLink.is_primary.is_(True),
        ))
        persisted_title = session.get(CatalogTitle, title_id)
        persisted_video = session.get(Video, video_id)
        assert persisted.external_id == "202"
        assert persisted_title.local_title == "Serie 1"
        assert persisted_video.relative_path == unchanged_video[3]


def test_title_language_preference_cookie_survives_recreated_app(tmp_path):
    settings = Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'preference.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    )
    first_app = create_app(settings)
    endpoint = next(
        route.endpoint for route in first_app.routes
        if getattr(route, "path", None) == "/preferences/title-name"
    )

    default_request = Request({
        "type": "http", "app": first_app, "method": "GET", "path": "/",
        "root_path": "", "scheme": "http", "query_string": b"", "headers": [],
        "server": ("testserver", 80), "client": ("testclient", 50000),
    })
    assert get_preferred_title_language(default_request) == "romaji"

    response = endpoint(preference=" EnGLish ", return_to="/hierarchy-review")

    assert response.status_code == 303
    assert response.headers["location"] == "/hierarchy-review"
    parsed_cookie = SimpleCookie()
    parsed_cookie.load(response.headers["set-cookie"])
    stored_preference = parsed_cookie[PREFERRED_TITLE_LANGUAGE_COOKIE].value
    assert stored_preference == "english"
    assert stored_preference in TITLE_NAME_PREFERENCE_LABELS
    assert "Max-Age=31536000" in response.headers["set-cookie"]

    external_response = endpoint(
        preference="native", return_to="https://evil.example"
    )
    assert external_response.status_code == 303
    assert external_response.headers["location"] == "/"
    assert (
        f"{PREFERRED_TITLE_LANGUAGE_COOKIE}=native"
        in external_response.headers["set-cookie"]
    )

    cookie = f"{PREFERRED_TITLE_LANGUAGE_COOKIE}=english".encode()
    for web_app in (first_app, create_app(settings)):
        request = Request({
            "type": "http", "app": web_app, "method": "GET", "path": "/",
            "root_path": "", "scheme": "http", "query_string": b"",
            "headers": [(b"cookie", cookie)], "server": ("testserver", 80),
            "client": ("testclient", 50000),
        })
        assert get_preferred_title_language(request) == "english"


def test_title_language_preference_rejects_unknown_cookie_value(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'invalid-preference.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/preferences/title-name"
    )

    with pytest.raises(HTTPException) as exc_info:
        endpoint(preference="klingon", return_to="/")

    assert exc_info.value.status_code == 400


def test_collection_and_hierarchy_review_share_cookie_title_preference(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'shared-title.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Ansatsu Kyoushitsu", normalized_local_title="ansatsu kyoushitsu",
            relative_root_path="Anime/Ansatsu Kyoushitsu", hierarchy_status="review_required",
        )
        title = CatalogTitle(
            collection=collection, local_title="Serie 1", normalized_local_title="serie 1",
            relative_root_path="Anime/Ansatsu Kyoushitsu/Serie 1", part_type="season",
            season_number=1, season_label="S1", metadata_status="linked_manual",
            metadata_record=TitleMetadata(
                display_title="Assassination Classroom",
                title_english="Assassination Classroom",
                title_romaji="Ansatsu Kyoushitsu", title_native="暗殺教室",
            ),
        )
        video = Video(
            relative_path="Anime/Ansatsu Kyoushitsu/Serie 1/Ansatsu Kyoushitsu 01.mp4",
            root_folder="Anime", filename="Ansatsu Kyoushitsu 01.mp4",
            size=1, mtime_ns=1, file_type="episode", local_episode_number=1,
            season_episode_number=1, catalog_title=title, catalog_collection=collection,
        )
        session.add(video)
        session.commit()
        collection_id = collection.id
        title_id = title.id
    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }
    cookie = f"{PREFERRED_TITLE_LANGUAGE_COOKIE}=native".encode()

    def request(path):
        return Request({
            "type": "http", "app": web_app, "method": "GET", "path": path,
            "root_path": "", "scheme": "http", "query_string": b"",
            "headers": [(b"cookie", cookie)], "server": ("testserver", 80),
            "client": ("testclient", 50000),
        })

    collection_page = endpoints["/collections/{collection_id}"](
        request(f"/collections/{collection_id}"), collection_id
    ).body.decode()
    hierarchy_page = endpoints["/hierarchy-review/{collection_id}"](
        request(f"/hierarchy-review/{collection_id}"), collection_id
    ).body.decode()

    assert f'href="/titles/{title_id}?' in collection_page
    assert ">暗殺教室</a>" in collection_page
    assert f'href="/titles/{title_id}">暗殺教室</a>' in hierarchy_page
    assert "Lokální část: <strong>Serie 1</strong>" in hierarchy_page


def test_ansatsu_hierarchy_review_summary_before_and_after_zero_separation(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'ansatsu-ui.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Ansatsu Kyoushitsu (Z15-Z16)",
            normalized_local_title="ansatsu kyoushitsu z15 z16",
            relative_root_path="Anime/Ansatsu Kyoushitsu (Z15-Z16)",
            hierarchy_status="review_required",
        )
        season = CatalogTitle(
            collection=collection, local_title="Serie 1",
            normalized_local_title="serie 1",
            relative_root_path=f"{collection.relative_root_path}/Serie 1",
            part_type="season", season_number=1, season_label="S1",
        )
        for number in range(23):
            session.add(Video(
                relative_path=f"{season.relative_root_path}/Ansatsu Kyoushitsu {number:02}.mp4",
                root_folder="Anime", filename=f"Ansatsu Kyoushitsu {number:02}.mp4",
                size=1, mtime_ns=1, file_type="other" if number == 0 else "episode",
                local_episode_number=number or None,
                season_episode_number=number or None,
                episode_number_source="nonstandard_zero" if number == 0 else "filename",
                catalog_title=season, catalog_collection=collection,
            ))
        session.commit()
        collection_id = collection.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )

    def render():
        request = Request({
            "type": "http", "app": web_app, "method": "GET",
            "path": f"/hierarchy-review/{collection_id}", "root_path": "",
            "scheme": "http", "query_string": b"", "headers": [],
            "server": ("testserver", 80), "client": ("testclient", 50000),
        })
        return endpoint(request, collection_id).body.decode()

    before = render()
    assert ">Ansatsu Kyoushitsu</a>" in before
    assert "Lokální část: <strong>Serie 1</strong>" in before
    assert "<dt>Fyzických videí</dt><dd>23</dd>" in before
    assert "<dt>Logických standardních epizod</dt><dd>22</dd>" in before
    assert "<dt>Očíslováno</dt><dd>22/22</dd>" in before
    assert "<dt>Rozsah</dt><dd>E1–E22</dd>" in before
    assert "<dt>Nestandardní</dt><dd>1</dd>" in before
    assert "Nestandardní epizoda: 00" in before

    with web_app.state.sessions() as session:
        zero = session.scalar(select(Video).where(Video.local_episode_number.is_(None)))
        preview = separate_nonstandard_videos(
            session, collection_id, [zero.id], local_title="Preview", part_type="preview",
        )
        session.commit()
        preview_id = preview.id

    after = render()
    season_block = after.split('id="title-1"', 1)[1].split("</article>", 1)[0]
    preview_block = after.split(f'id="title-{preview_id}"', 1)[1].split("</article>", 1)[0]
    assert "<dt>Fyzických videí</dt><dd>22</dd>" in season_block
    assert "<dt>Očíslováno</dt><dd>22/22</dd>" in season_block
    assert "Stav číslování: vyžaduje kontrolu" not in season_block
    assert "<dt>Fyzických videí</dt><dd>1</dd>" in preview_block
    assert "Doplňkový obsah – standardní completeness se nepoužije" in preview_block
    assert "Bez externích metadat" in preview_block


def test_dxd_manual_e01_override_updates_effective_hierarchy_review_groups(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'dxd-effective-numbering.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="High School DxD Hero",
            normalized_local_title="high school dxd hero",
            relative_root_path="Anime/High School DxD Hero",
        )
        season = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/High School DxD Hero/Season 1",
            part_type_manual="season", season_number_manual=1,
            season_label_manual="S1", hierarchy_manual_override=True,
        )
        for number in range(2, 14):
            season.videos.append(Video(
                relative_path=f"{season.relative_root_path}/Episode {number:02}.mkv",
                root_folder="Anime", filename=f"Episode {number:02}.mkv",
                size=1, mtime_ns=1, catalog_collection=collection,
            ))
        zero = Video(
            relative_path=(
                f"{season.relative_root_path}/High School DxD Hero - 00.mkv"
            ),
            root_folder="Anime", filename="High School DxD Hero - 00.mkv",
            size=1, mtime_ns=1, catalog_title=season,
            catalog_collection=collection,
        )
        session.add(collection)
        session.flush()
        recalculate_title_numbering(season, list(season.videos))
        refresh_collection_state(collection, recalculate=False)
        session.commit()
        collection_id, title_id, zero_id = collection.id, season.id, zero.id

    review_endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )

    def render():
        return review_endpoint(
            web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
        ).body.decode()

    before = render()
    before_part = before.split(f'id="title-{title_id}"', 1)[1].split("</article>", 1)[0]
    assert "Standardní epizody (12)" in before_part
    assert "Nestandardní obsah (1)" in before_part
    assert "Nestandardní epizoda: 00" in before_part
    assert "Vyžaduje zařazení" in before_part

    update_endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/videos/{video_id}/episode-number"
    )
    response = update_endpoint(
        zero_id, manual_episode_number="1", filter_name="all", q="", sort="",
        direction="", detail_sort="", detail_direction="",
    )
    assert response.status_code == 303

    after = render()
    after_part = after.split(f'id="title-{title_id}"', 1)[1].split("</article>", 1)[0]
    assert "<dt>Fyzických videí</dt><dd>13</dd>" in after_part
    assert "<dt>Logických standardních epizod</dt><dd>13</dd>" in after_part
    assert "<dt>Očíslováno</dt><dd>13/13</dd>" in after_part
    assert "<dt>Rozsah</dt><dd>E1–E13</dd>" in after_part
    assert "<dt>Unknown</dt><dd>0</dd>" in after_part
    assert "<dt>Nestandardní</dt><dd>0</dd>" in after_part
    assert "Číslování vyřešeno" in after_part
    assert "Standardní epizody (13)" in after_part
    assert "Nestandardní obsah" not in after_part
    assert "Vyžaduje zařazení" not in after_part

    with web_app.state.sessions() as session:
        stored_zero = session.get(Video, zero_id)
        stored_collection = session.get(CatalogCollection, collection_id)
        assert stored_zero.episode_number_manual_override == 1
        assert stored_zero.season_episode_number == 1
        assert stored_zero.episode_number_source == "manual"
        assert detect_episode_number(stored_zero.filename).kind == "zero"
        assert stored_zero.filename == "High School DxD Hero - 00.mkv"
        assert stored_collection.hierarchy_status == "verified"
        assert stored_collection.hierarchy_note is None


def test_bungo_bulk_duplicate_resolution_keeps_physical_cleanup_warning(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'bungo-duplicates.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Bungo to Alchemist - Shinpan no Haguruma",
            normalized_local_title="bungo to alchemist shinpan no haguruma",
            relative_root_path="Anime/Bungo", hierarchy_status="review_required",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1", relative_root_path="Anime/Bungo/Season 1",
            part_type_manual="season", season_number_manual=1,
            season_label_manual="S1", hierarchy_manual_override=True,
        )
        for number in range(1, 14):
            for filename, size in (
                (f"Bungo - {number:02}.mkv", 1_000_000_000 + number),
                (f"Bungo {number:02}.mp4", 2_000_000_000 + number),
            ):
                Video(
                    relative_path=f"Anime/Bungo/Season 1/{filename}",
                    root_folder="Anime", filename=filename, size=size, mtime_ns=1,
                    duration=1440, video_codec="h264", width=1920, height=1080,
                    local_episode_number=number, season_episode_number=number,
                    catalog_title=title, catalog_collection=collection,
                )
        session.add(collection)
        session.commit()
        collection_id, title_id = collection.id, title.id

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    before = endpoints["/hierarchy-review/{collection_id}"](
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()
    assert "Vyřešit duplicity · podezření (13 skupin)" in before
    assert 'class="automatic-duplicate-badge"' in before
    assert before.count('name="group_key"') == 13
    assert before.count('action="/hierarchy-review/1/duplicates/confirm-bulk"') == 1
    assert "Délka 24:00 · 1920×1080 · h264" in before

    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        groups = unresolved_duplicate_groups(list(collection.titles[0].videos))
        form_items = [("confirm_duplicate", "true")]
        for group in groups:
            key = f"{title_id}-{group.episode_number}"
            form_items.append(("group_key", key))
            form_items.extend(
                (f"video_ids_{key}", str(video.id)) for video in group.videos
            )
            form_items.append((f"primary_{key}", str(group.videos[0].id)))
    body = urlencode(form_items).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http", "app": web_app, "method": "POST",
        "path": f"/hierarchy-review/{collection_id}/duplicates/confirm-bulk",
        "root_path": "", "scheme": "http", "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "server": ("testserver", 80), "client": ("testclient", 50000),
    }, receive)
    response = asyncio.run(
        endpoints["/hierarchy-review/{collection_id}/duplicates/confirm-bulk"](
            request, collection_id,
        )
    )
    assert response.status_code == 303

    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        assert collection.hierarchy_status == "review_required"
        assert collection.hierarchy_note == CONFIRMED_DUPLICATES_REVIEW_REASON

    after = endpoints["/hierarchy-review/{collection_id}"](
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()
    assert "<dt>Fyzických videí</dt><dd>26</dd>" in after
    assert "<dt>Logických standardních epizod</dt><dd>13</dd>" in after
    assert "<dt>Očíslováno</dt><dd>13/13</dd>" in after
    assert "<dt>Rozsah</dt><dd>E1–E13</dd>" in after
    assert "<dt>Potvrzených duplicit</dt><dd>13</dd>" in after
    assert "Číslování vyřešeno" in after
    assert "Fyzické duplicity vyžadují vyřešení: 13" in after
    assert "Potvrzené duplicity (13)" in after
    assert "Vyřešit duplicity · podezření" not in after
    assert 'class="automatic-duplicate-badge"' not in after

    title_detail = endpoints["/titles/{catalog_title_id}"](
        web_request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()
    assert title_detail.count("+ 1 potvrzená duplicitní kopie") == 13
    assert "Fyzický cleanup dosud nebyl proveden." in title_detail


def test_manual_duplicate_endpoint_marks_and_clears_without_other_changes(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'manual-duplicate.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", hierarchy_status="verified",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1", relative_root_path="Anime/Show/Season 1",
            part_type_manual="season", season_number_manual=1,
            season_label_manual="S1", hierarchy_manual_override=True,
        )
        title.metadata_record = TitleMetadata(
            display_title="Remote Show", metadata_provider="anilist",
            metadata_external_id="123",
        )
        video = Video(
            relative_path="Anime/Show/Season 1/Show - 01.mkv", root_folder="Anime",
            filename="Show - 01.mkv", size=100, mtime_ns=1,
            content_type_manual="recap", season_episode_number=1,
            catalog_title=title, catalog_collection=collection,
        )
        session.add(collection)
        session.commit()
        video_id, title_id, collection_id = video.id, title.id, collection.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/videos/{video_id}/duplicate-status-manual"
    )
    response = endpoint(
        video_id, duplicate_status_manual="suspected",
        return_to=f"/titles/{title_id}#video-{video_id}",
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/titles/{title_id}#video-{video_id}"

    external_response = endpoint(
        video_id, duplicate_status_manual="suspected",
        return_to="https://evil.example",
    )
    assert external_response.status_code == 303
    assert external_response.headers["location"] == "/"

    with web_app.state.sessions() as session:
        stored = session.get(Video, video_id)
        assert stored.duplicate_status_manual == "suspected"
        assert stored.duplicate_of_video_id is None
        assert stored.content_type_manual == "recap"
        assert stored.catalog_title_id == title_id
        assert stored.catalog_collection_id == collection_id
        assert stored.catalog_title.metadata_record.display_title == "Remote Show"

    detail_endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/titles/{catalog_title_id}"
    )
    rendered = detail_endpoint(
        web_request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()
    assert "Ruční podezření na duplicitu" in rendered
    assert "Zrušit ruční označení" in rendered
    assert "primary nebylo vybráno" in rendered

    response = endpoint(
        video_id, duplicate_status_manual="",
        return_to=f"/hierarchy-review/{collection_id}#manual-duplicate-video-{video_id}",
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/hierarchy-review/{collection_id}#manual-duplicate-video-{video_id}"
    )
    with web_app.state.sessions() as session:
        stored = session.get(Video, video_id)
        assert stored.duplicate_status_manual is None
        assert stored.duplicate_of_video_id is None
        assert stored.content_type_manual == "recap"


def test_all_duplicates_filter_renders_unresolved_and_confirmed_not_manual_only(
    tmp_path,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'all-duplicates.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", hierarchy_status="verified",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1",
        )

        def video(filename, episode, *, suspected=False):
            return Video(
                relative_path=f"Anime/Show/Season 1/{filename}",
                root_folder="Anime", filename=filename, size=1, mtime_ns=1,
                file_type="episode", season_episode_number=episode,
                duplicate_status_manual="suspected" if suspected else None,
                catalog_title=title, catalog_collection=collection,
            )

        unresolved = video("AUTO-ONE.mkv", 1)
        unresolved_suspected = video("AUTO-TWO-SUSPECTED.mkv", 1, suspected=True)
        normal = video("NORMAL.mkv", 2)
        manual_only = video("MANUAL-ONLY.mkv", 3, suspected=True)
        primary = video("PRIMARY.mkv", 4)
        confirmed = video("CONFIRMED-SUSPECTED.mkv", 4, suspected=True)
        session.add_all([
            unresolved, unresolved_suspected, normal, manual_only, primary, confirmed,
        ])
        session.flush()
        confirmed.duplicate_of_video_id = primary.id
        session.commit()
        title_id = title.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/titles/{catalog_title_id}"
    )
    rendered = endpoint(
        web_request(web_app, f"/titles/{title_id}"), title_id,
        filter_name="all-duplicates",
    ).body.decode()

    assert "Všechny duplicity" in rendered
    assert "AUTO-ONE.mkv" in rendered
    assert "AUTO-TWO-SUSPECTED.mkv" in rendered
    assert "CONFIRMED-SUSPECTED.mkv" in rendered
    assert "NORMAL.mkv" not in rendered
    assert "MANUAL-ONLY.mkv" not in rendered
    assert "PRIMARY.mkv" not in rendered
    assert rendered.count("Automaticky nalezený problém") == 2
    assert "Potvrzená duplicita" in rendered
    assert "Ruční podezření na duplicitu" in rendered
    assert "Neplatný vztah duplicity" not in rendered


def test_unreviewed_manual_duplicate_ui_does_not_mean_not_duplicate():
    video = Video(
        id=1, relative_path="Anime/Show/E01.mkv", root_folder="Anime",
        filename="E01.mkv", size=1, mtime_ns=1,
    )

    rendered = str(
        templates.env.get_template("_manual_duplicate.html").module
        .manual_duplicate_control(video, "/titles/1#video-1")
    )

    assert video.duplicate_status_manual is None
    assert "Označit jako podezřelou duplicitu" in rendered
    assert "Ruční podezření na duplicitu" not in rendered
    assert "Není duplicita" not in rendered


def test_manual_duplicate_review_section_is_collapsed_and_shows_counts():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="Season 1",
        normalized_local_title="season 1", relative_root_path="Anime/Show/Season 1",
    )
    videos = [
        Video(
            id=identifier, relative_path=f"Anime/Show/Season 1/E{identifier:02}.mkv",
            root_folder="Anime", filename=f"E{identifier:02}.mkv", size=1,
            mtime_ns=1, duplicate_status_manual=("suspected" if identifier == 2 else None),
            catalog_title=title, catalog_collection=collection,
        )
        for identifier in (1, 2, 3)
    ]
    rendered = templates.env.get_template("hierarchy_review_detail.html").render(
        request=type("Request", (), {
            "url_for": lambda self, *args, **kwargs: "/static/style.css",
        })(),
        collection=collection, videos=videos, numbering_unknown=0,
        nonstandard_videos=[], unassigned_videos={
            "standard": [], "supplemental": [], "nonstandard": [], "unknown": [],
        },
        message=None, error=None, part_confirmation_suggestion=None,
        title_numbering=[], metadata_status_labels={}, simple_rows=[],
        definitions_json="[]", external_search_candidates=[], external_candidates=[],
        preview=None, preview_rows=[], available_collections=[],
        supplementary_suggestions=[], supplementary_suggestion_by_video={},
        duplicate_candidate_video_ids=set(), confirmed_duplicate_video_ids=set(),
    )
    opening_tag = rendered.split('id="manual-duplicates"', 1)[0].rsplit("<", 1)[1]

    assert "details class=" in opening_tag
    assert " open" not in opening_tag
    assert "Ruční podezření na duplicitu · 3 videí · označeno: 1" in rendered
    assert "Toto označení je nezávislá poznámka" in rendered
    assert "Zrušit ruční označení" in rendered


def test_confirmed_duplicate_ui_keeps_manual_suspicion_visually_secondary(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'confirmed-manual-duplicate.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", hierarchy_status="review_required",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1", relative_root_path="Anime/Show/Season 1",
            part_type_manual="season", hierarchy_manual_override=True,
        )
        first = Video(
            relative_path="Anime/Show/Season 1/Show - 01.mkv", root_folder="Anime",
            filename="Show - 01.mkv", size=100, mtime_ns=1,
            season_episode_number=1, catalog_title=title, catalog_collection=collection,
        )
        second = Video(
            relative_path="Anime/Show/Season 1/Show 01.mp4", root_folder="Anime",
            filename="Show 01.mp4", size=200, mtime_ns=2,
            season_episode_number=1, duplicate_status_manual="suspected",
            catalog_title=title, catalog_collection=collection,
        )
        session.add(collection)
        session.flush()
        confirm_duplicate_videos(
            session, collection.id, [first.id, second.id], first.id,
        )
        session.commit()
        collection_id, second_id = collection.id, second.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )
    rendered = endpoint(
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()
    control = rendered.split(
        f'id="manual-duplicate-video-{second_id}"', 1
    )[1].split("</div>", 1)[0]

    assert "Potvrzená duplicita" in control
    assert "Ruční podezření na duplicitu" in control
    assert "samostatná starší poznámka" in control
    assert "Zrušit ruční označení" in control
    assert "Označit jako podezřelou duplicitu" not in control
    assert "Automaticky nalezený problém" not in control


def test_hierarchy_review_offers_season_specific_ova_reassignment(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'supplementary-review.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="High School DxD",
            normalized_local_title="high school dxd",
            relative_root_path="Anime/High School DxD", hierarchy_status="review_required",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/High School DxD/Season 1",
            part_type_manual="season", season_number_manual=1,
            season_label_manual="S1", hierarchy_manual_override=True,
        )
        Video(
            relative_path="Anime/High School DxD/Season 1/High School DxD - 01.mkv",
            root_folder="Anime", filename="High School DxD - 01.mkv",
            size=1, mtime_ns=1, local_episode_number=1, season_episode_number=1,
            catalog_title=title, catalog_collection=collection,
        )
        Video(
            relative_path=(
                "Anime/High School DxD/Season 1/High School DxD - OVA 01.mkv"
            ),
            root_folder="Anime", filename="High School DxD - OVA 01.mkv",
            size=1, mtime_ns=1, catalog_title=title, catalog_collection=collection,
        )
        session.add(collection)
        session.commit()
        collection_id = collection.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )
    rendered = endpoint(
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    assert "Vyřešit duplicity · podezření" not in rendered
    assert "AnimeDB doporučuje" in rendered
    assert "Doporučené oddělení" in rendered
    assert "OVA 01" in rendered
    assert 'data-part-type="ova"' in rendered
    assert 'data-season-number=""' in rendered
    assert "Použít doporučení" in rendered
    assert "Oddělit do nové části" in rendered
    assert "Správa zařazení" in rendered
    assert "<dt>Očíslováno</dt><dd>1/1</dd>" in rendered


def test_hataraku_sp_recommendation_is_read_only_prefill_for_existing_form(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hataraku-recommendation.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Hataraku Saibou", normalized_local_title="hataraku saibou",
            relative_root_path="Anime/Hataraku Saibou", hierarchy_status="review_required",
        )
        season = CatalogTitle(
            collection=collection, local_title="Serie 1",
            normalized_local_title="serie 1",
            relative_root_path="Anime/Hataraku Saibou/Serie 1",
            part_type="season", season_number=1, season_label="S1",
        )
        special = Video(
            relative_path=(
                "Anime/Hataraku Saibou/Serie 1/"
                "S01E14 [SP]-The Common Cold.mkv"
            ),
            root_folder="Anime", filename="S01E14 [SP]-The Common Cold.mkv",
            size=1, mtime_ns=1, catalog_title=season, catalog_collection=collection,
        )
        session.add(collection)
        session.commit()
        collection_id, season_id, video_id = collection.id, season.id, special.id
        before = (
            special.catalog_title_id, special.content_type_manual,
            special.episode_number_manual_override, special.season_episode_number,
            season.part_type_manual, season.season_number_manual,
            season.hierarchy_manual_override,
        )

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )
    rendered = endpoint(
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    assert "AnimeDB doporučuje" in rendered
    assert "Doporučené oddělení" in rendered
    assert "S01E14 [SP]-The Common Cold.mkv" in rendered
    assert "Special · související S01 · canonical číslo neurčeno" in rendered
    assert "Původní filename hint: S01E14" in rendered
    assert "Název z filename: The Common Cold" in rendered
    assert "Canonical číslo: neurčeno" in rendered
    assert 'type="button" class="apply-assignment-recommendation"' in rendered
    assert f'data-video-ids="{video_id}"' in rendered
    assert 'data-part-type="special"' in rendered
    assert 'data-local-title="Specials"' in rendered
    assert 'data-season-number="1"' in rendered
    assert 'data-season-label="S1"' in rendered
    assert 'id="assignment-form"' in rendered
    assert f'action="/hierarchy-review/{collection_id}/manage-videos"' in rendered
    assert "Provést změnu zařazení" in rendered
    assert "form.elements.part_type.value=button.dataset.partType" in rendered
    assert "form.elements.season_number.value=button.dataset.seasonNumber" in rendered
    assert "form.elements.season_label.value=button.dataset.seasonLabel" in rendered
    assert "form.elements.start_episode" not in rendered
    assert '<select name="part_type">' in rendered
    assert 'min="1" name="season_number"' in rendered
    assert 'maxlength="50" name="season_label"' in rendered
    assert "zatím nebylo nic uloženo" in rendered
    assert "Pravděpodobně doplňkový obsah" not in rendered

    with web_app.state.sessions() as session:
        special = session.get(Video, video_id)
        season = session.get(CatalogTitle, season_id)
        assert (
            special.catalog_title_id, special.content_type_manual,
            special.episode_number_manual_override, special.season_episode_number,
            season.part_type_manual, season.season_number_manual,
            season.hierarchy_manual_override,
        ) == before


def test_fractional_video_can_be_classified_directly_without_confirming_title(
    tmp_path,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'direct-video-classification.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Kandagawa Jet Girls P19",
            normalized_local_title="kandagawa jet girls p19",
            relative_root_path="Anime/Kandagawa Jet Girls P19",
            hierarchy_status="review_required",
            hierarchy_note="Nestandardní číslování vyžaduje ruční zařazení.",
        )
        title = CatalogTitle(
            collection=collection, local_title="Kandagawa Jet Girls P19",
            normalized_local_title="kandagawa jet girls p19",
            relative_root_path="Anime/Kandagawa Jet Girls P19",
        )
        for number in range(1, 13):
            Video(
                relative_path=(
                    f"Anime/Kandagawa Jet Girls P19/"
                    f"Kandagawa Jet Girls - {number:02}.mkv"
                ),
                root_folder="Anime",
                filename=f"Kandagawa Jet Girls - {number:02}.mkv",
                size=number, mtime_ns=number, local_episode_number=number,
                season_episode_number=number, absolute_episode_number=number,
                catalog_title=title, catalog_collection=collection,
            )
        fractional = Video(
            relative_path=(
                "Anime/Kandagawa Jet Girls P19/Kandagawa Jet Girls - 04.5.mkv"
            ),
            root_folder="Anime", filename="Kandagawa Jet Girls - 04.5.mkv",
            size=45, mtime_ns=45, duplicate_status_manual="suspected",
            manual_hardsub_cs=True, manual_hardsub_verified_at=utc_now(),
            catalog_title=title, catalog_collection=collection,
        )
        session.add(collection)
        refresh_collection_state(collection)
        session.commit()
        collection_id, title_id, video_id = collection.id, title.id, fractional.id
        title_state = (
            title.part_type_manual, title.season_number_manual,
            title.season_label_manual, title.sort_order_manual,
            title.hierarchy_manual_override, title.hierarchy_verified_at,
        )
        unrelated_video_state = (
            fractional.duplicate_status_manual, fractional.manual_hardsub_cs,
            fractional.manual_hardsub_verified_at.replace(tzinfo=None),
            fractional.relative_path,
            fractional.catalog_title_id, fractional.catalog_collection_id,
        )

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    detail_endpoint = endpoints["/hierarchy-review/{collection_id}"]
    rendered = detail_endpoint(
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    assert "stav <strong>review_required</strong>" in rendered
    assert "<dt>Logických standardních epizod</dt><dd>12</dd>" in rendered
    assert "<dt>Rozsah</dt><dd>E1–E12</dd>" in rendered
    assert "<dt>Nestandardní</dt><dd>1</dd>" in rendered
    assert "Doporučené zařazení: Season 1 (S1)" in rendered
    assert "Tento návrh hierarchie části je oddělený" in rendered
    assert "Potvrzení návrhu Season 1 (S1) nevyřeší nestandardní číslování" in rendered
    block = rendered.split(f'id="nonstandard-video-{video_id}"', 1)[1].split(
        "</li>", 1,
    )[0]
    assert "Kandagawa Jet Girls - 04.5.mkv" in block
    assert "Nestandardní epizoda: 4.5" in block
    assert "Toto video je důvod kontroly a zde ho lze vyřešit." in block
    assert "Vyřešit jako" in block
    assert "Potvrdit řešení" in block
    expected_choices = tuple(value for value, _ in VIDEO_CONTENT_TYPE_CHOICES)
    assert select_option_values(block, "content_type") == [("", *expected_choices)]
    assert "Automaticky / zrušit ruční klasifikaci" not in block

    path = f"/hierarchy-review/{collection_id}/manage-videos"
    response = asyncio.run(endpoints["/hierarchy-review/{collection_id}/manage-videos"](
        post_form_request(web_app, path, [
            ("video_ids", str(video_id)),
            ("operation", "classify"),
            ("content_type", "recap"),
        ]),
        collection_id,
    ))

    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    assert target.fragment == "operation-result"
    message = parse_qs(target.query)["message"][0]
    assert message == "Video bylo ručně klasifikováno jako recap."
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        title = session.get(CatalogTitle, title_id)
        fractional = session.get(Video, video_id)
        assert fractional.content_type_manual == "recap"
        assert (
            fractional.duplicate_status_manual, fractional.manual_hardsub_cs,
            fractional.manual_hardsub_verified_at.replace(tzinfo=None),
            fractional.relative_path,
            fractional.catalog_title_id, fractional.catalog_collection_id,
        ) == unrelated_video_state
        assert (
            title.part_type_manual, title.season_number_manual,
            title.season_label_manual, title.sort_order_manual,
            title.hierarchy_manual_override, title.hierarchy_verified_at,
        ) == title_state
        assert (title.part_type, title.season_number, title.season_label) == (
            "season", 1, "S1",
        )
        assert collection.hierarchy_status == "automatic"
        assert collection.hierarchy_note is None

    after = detail_endpoint(
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
        message=message,
    ).body.decode()
    assert (
        '<div class="notice success" id="operation-result">'
        "Video bylo ručně klasifikováno jako recap.</div>"
    ) in after
    assert "Důvod kontroly: Nestandardní číslování" not in after
    assert "stav <strong>automatic</strong>" in after
    assert "· Hierarchie ověřena" not in after
    assert "<dt>Nestandardní</dt><dd>0</dd>" in after
    assert "Číslování vyřešeno" in after

    invalid_request = post_form_request(web_app, path, [
        ("video_ids", str(video_id)),
        ("operation", "classify"),
        ("content_type", "film"),
    ])
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(endpoints["/hierarchy-review/{collection_id}/manage-videos"](
            invalid_request, collection_id,
        ))
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Neplatný typ doplňkového obsahu."
    with web_app.state.sessions() as session:
        assert session.get(Video, video_id).content_type_manual == "recap"


@pytest.mark.parametrize(
    ("filename", "file_type", "manual_type", "label", "position"),
    [
        ("Anime - 05.5.mkv", "other", "recap", "Recap · ručně zařazeno", "5.5"),
        ("Anime - 14.5.mkv", "other", "ova", "OVA · ručně zařazeno", "14.5"),
        ("Anime Special.mkv", "special", None, "special", None),
    ],
)
def test_effective_video_content_display_prefers_manual_classification(
    filename, file_type, manual_type, label, position,
):
    video = Video(
        relative_path=f"Anime/{filename}", root_folder="Anime", filename=filename,
        size=1, mtime_ns=1, file_type=file_type,
        content_type_manual=manual_type,
    )

    display = effective_video_content_display(video)

    assert display.display_label == label
    assert display.noncanonical_position == position
    assert display.is_manual is (manual_type is not None)


def test_fractional_supplementary_position_and_effective_type_match_in_views(
    tmp_path,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'fractional-content-display.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Arifureta Shokugyou de Sekai Saikyou",
            normalized_local_title="arifureta shokugyou de sekai saikyou",
            relative_root_path="Anime/Arifureta Shokugyou de Sekai Saikyou",
        )
        title = CatalogTitle(
            collection=collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path=(
                "Anime/Arifureta Shokugyou de Sekai Saikyou/Season 1"
            ),
            part_type="season", season_number=1, season_label="S1",
        )
        for number in range(1, 13):
            standard_video = Video(
                relative_path=(
                    "Anime/Arifureta Shokugyou de Sekai Saikyou/Season 1/"
                    f"Arifureta - {number:02}.mkv"
                ),
                root_folder="Anime", filename=f"Arifureta - {number:02}.mkv",
                size=number, mtime_ns=number, file_type="episode",
                local_episode_number=number, season_episode_number=number,
                absolute_episode_number=number, catalog_title=title,
                catalog_collection=collection,
            )
            if number == 1:
                automatic_video = standard_video
        recap = Video(
            relative_path=(
                "Anime/Arifureta Shokugyou de Sekai Saikyou/Season 1/"
                "Arifureta Shokugyou de Sekai Saikyou - 05.5.mkv"
            ),
            root_folder="Anime",
            filename="Arifureta Shokugyou de Sekai Saikyou - 05.5.mkv",
            size=55, mtime_ns=55, file_type="other", content_type_manual="recap",
            catalog_title=title, catalog_collection=collection,
        )
        ova = Video(
            relative_path=(
                "Anime/Arifureta Shokugyou de Sekai Saikyou/Season 1/"
                "Arifureta Shokugyou de Sekai Saikyou - 14.5.mkv"
            ),
            root_folder="Anime",
            filename="Arifureta Shokugyou de Sekai Saikyou - 14.5.mkv",
            size=145, mtime_ns=145, file_type="other", content_type_manual="ova",
            catalog_title=title, catalog_collection=collection,
        )
        session.add(collection)
        refresh_collection_state(collection)
        session.commit()
        collection_id, title_id = collection.id, title.id
        recap_id, ova_id = recap.id, ova.id
        automatic_video_id = automatic_video.id
        status_before = (collection.hierarchy_status, collection.hierarchy_note)
        summary = summarize_title_numbering(title.videos, title)
        assert (
            summary.standard_total, summary.nonstandard,
            summary.resolved_supplemental,
        ) == (12, 0, 2)

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    hierarchy_html = endpoints["/hierarchy-review/{collection_id}"](
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()
    title_html = endpoints["/titles/{catalog_title_id}"](
        web_request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()

    assert "5.5 · Recap · ručně zařazeno" in hierarchy_html
    assert "14.5 · OVA · ručně zařazeno" in hierarchy_html
    assert "<dt>Logických standardních epizod</dt><dd>12</dd>" in hierarchy_html
    assert "<dt>Nestandardní</dt><dd>0</dd>" in hierarchy_html
    assert "<dt>Zařazený doplňkový obsah</dt><dd>2</dd>" in hierarchy_html

    recap_row = title_html.split(f'id="video-{recap_id}"', 1)[1].split(
        "</tr>", 1,
    )[0]
    ova_row = title_html.split(f'id="video-{ova_id}"', 1)[1].split(
        "</tr>", 1,
    )[0]
    automatic_row = title_html.split(
        f'id="video-{automatic_video_id}"', 1,
    )[1].split("</tr>", 1)[0]
    assert "<strong>5.5</strong><small>Nekanonická pozice</small>" in recap_row
    assert "Recap · ručně zařazeno" in recap_row
    assert ">other<" not in recap_row
    assert "E5.5" not in recap_row
    assert "<strong>14.5</strong><small>Nekanonická pozice</small>" in ova_row
    assert "OVA · ručně zařazeno" in ova_row
    assert ">other<" not in ova_row
    assert "E14.5" not in ova_row
    assert '<td class="compact-column">episode</td>' in automatic_row

    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        recap = session.get(Video, recap_id)
        ova = session.get(Video, ova_id)
        assert (collection.hierarchy_status, collection.hierarchy_note) == status_before
        assert recap.content_type_manual == "recap"
        assert ova.content_type_manual == "ova"
        for video in (recap, ova):
            assert (
                video.local_episode_number, video.season_episode_number,
                video.absolute_episode_number,
            ) == (None, None, None)


@pytest.mark.parametrize("episode_count", [15, 24])
def test_hierarchy_review_renders_nonblocking_long_sequence_notice(
    tmp_path, episode_count,
):
    local_title = (
        "Mugen no Juunin - Immortal P19" if episode_count == 15 else "Long Show"
    )
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / f'long-{episode_count}.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title=local_title, normalized_local_title=local_title.casefold(),
            relative_root_path=f"Anime/{local_title}",
        )
        title = CatalogTitle(
            collection=collection, local_title=local_title,
            normalized_local_title=local_title.casefold(),
            relative_root_path=collection.relative_root_path, part_type="title",
        )
        for number in range(1, episode_count + 1):
            Video(
                relative_path=(
                    f"Anime/{local_title}/{local_title} - {number:02}.mkv"
                ),
                root_folder="Anime", filename=f"{local_title} - {number:02}.mkv",
                size=1, mtime_ns=number, catalog_title=title,
                catalog_collection=collection,
            )
        session.add(collection)
        refresh_collection_state(collection)
        session.commit()
        collection_id = collection.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )
    rendered = endpoint(
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    assert "stav <strong>automatic</strong>" in rendered
    card = rendered.split('class="panel hierarchy-title-card"', 1)[1].split(
        "</article>", 1,
    )[0]
    assert 'class="notice soft-warning" role="note"' in card
    assert "ℹ Informativní upozornění:" in card
    assert (
        f"Delší souvislá řada E1–E{episode_count} bez explicitního dělení. "
        "Zkontrolujte případné rozdělení na sezóny nebo části."
    ) in card
    assert "Důvod kontroly:" not in rendered


def test_hierarchy_review_offers_existing_confirmation_and_split_for_over_24(
    tmp_path,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'very-long.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Very Long Show", normalized_local_title="very long show",
            relative_root_path="Anime/Very Long Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Very Long Show",
            normalized_local_title="very long show",
            relative_root_path=collection.relative_root_path, part_type="title",
        )
        for number in range(1, 26):
            Video(
                relative_path=(
                    f"Anime/Very Long Show/Very Long Show - {number:02}.mkv"
                ),
                root_folder="Anime", filename=f"Very Long Show - {number:02}.mkv",
                size=1, mtime_ns=number, catalog_title=title,
                catalog_collection=collection,
            )
        session.add(collection)
        refresh_collection_state(collection)
        session.commit()
        collection_id = collection.id

    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }
    rendered = endpoints["/hierarchy-review/{collection_id}"](
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    assert "stav <strong>review_required</strong>" in rendered
    assert "Neobvykle dlouhá souvislá řada: E1–E25" in rendered
    assert "Informativní upozornění:" not in rendered
    assert "Doporučené zařazení: Season 1 (S1)" in rendered
    assert "Potvrdit jako jednu sezónu" in rendered
    assert 'href="#manual-split"' in rendered
    assert 'id="manual-split"' in rendered

    response = endpoints["/hierarchy-review/{collection_id}/confirm-part"](
        collection_id, part_type_manual="season", season_number_manual="1",
        season_label_manual="S1", part_number_manual="", confirm_part=True,
    )
    assert response.status_code == 303
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        title = collection.titles[0]
        assert collection.hierarchy_status == "verified"
        assert title.part_type_manual == "season"
        assert title.season_number_manual == 1
        assert title.season_label_manual == "S1"
        assert title.hierarchy_manual_override is True
        assert title.hierarchy_verified_at is not None


def test_part_type_choices_are_shared_by_collection_and_hierarchy_review(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'part-type-choices.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", hierarchy_status="review_required",
        )
        title = CatalogTitle(
            collection=collection, local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", part_type="part",
            season_number=1, part_number=2, season_label="S1",
        )
        Video(
            relative_path="Anime/Show/Show 00.mkv", root_folder="Anime",
            filename="Show 00.mkv", size=1, mtime_ns=1,
            catalog_title=title, catalog_collection=collection,
        )
        session.add(collection)
        session.commit()
        collection_id = collection.id

    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }
    request = web_request(web_app, f"/collections/{collection_id}")
    collection_html = endpoints["/collections/{collection_id}"](
        request, collection_id, filter_name="all", q="", sort=None, direction=None,
    ).body.decode()
    review_html = endpoints["/hierarchy-review/{collection_id}"](
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    expected_part_types = tuple(value for value, _ in PART_TYPE_CHOICES)
    collection_choices = select_option_values(collection_html, "part_type_manual")
    review_manual_choices = select_option_values(review_html, "part_type_manual")
    review_split_choices = select_option_values(review_html, "part_type")
    video_choices = select_option_values(review_html, "content_type")

    assert collection_choices
    assert review_manual_choices
    assert review_split_choices
    assert all(tuple(value for value in choices if value) == expected_part_types
               for choices in collection_choices + review_manual_choices)
    assert all(choices == expected_part_types for choices in review_split_choices)
    assert all(choices == ("", *(value for value, _ in VIDEO_CONTENT_TYPE_CHOICES))
               for choices in video_choices)
    assert "Automaticky / zrušit ruční klasifikaci" in review_html
    assert "film" in expected_part_types
    assert "title" not in expected_part_types
    assert all("film" not in choices for choices in video_choices)
    assert (
        f'action="/collections/{collection_id}/titles/{title.id}/hierarchy"'
        in review_html
    )
    disabled_save = (
        'class="manual-hierarchy-save" type="submit" disabled'
    )
    assert disabled_save in collection_html
    assert disabled_save in review_html
    assert 'src="/static/hierarchy_fields.js"' in collection_html
    assert 'src="/static/hierarchy_fields.js"' in review_html
    assert 'name="part_number_manual"' in collection_html
    assert 'name="part_number_manual"' in review_html
    assert "Číslo sezóny" in collection_html
    assert "Číslo Part" in collection_html
    assert "Cour" not in collection_html
    assert "S1 · Part 2" in collection_html
    assert "S1 · Part 2" in review_html
    assert 'name="return_to" value="hierarchy_review"' in review_html
    assert "Typ celé části" in review_html
    assert "Klasifikace vybraných videí" in review_html


def test_disabled_buttons_have_shared_inactive_visual_style():
    stylesheet = (Path(__file__).parents[1] / "app/static/style.css").read_text()
    disabled_rule = stylesheet.split("button:disabled{", 1)[1].split("}", 1)[0]

    assert "background:#374151" in disabled_rule
    assert "color:#94a3b8" in disabled_rule
    assert "cursor:not-allowed" in disabled_rule
    assert "opacity:.72" in disabled_rule


def test_soft_sequence_warning_has_distinct_informational_style():
    stylesheet = (Path(__file__).parents[1] / "app/static/style.css").read_text()
    warning_rule = stylesheet.split(".soft-warning{", 1)[1].split("}", 1)[0]

    assert "background:#172554" in warning_rule
    assert "border:1px solid #3b82f6" in warning_rule
    assert "color:#bfdbfe" in warning_rule


def test_manual_split_apply_redirects_to_visible_success_message(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'manual-split-apply.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", hierarchy_status="review_required",
        )
        title = CatalogTitle(
            collection=collection, local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        videos = [Video(
            relative_path=f"Anime/Show/Show - {number:02}.mkv",
            root_folder="Anime", filename=f"Show - {number:02}.mkv",
            size=number, mtime_ns=number, local_episode_number=number,
            season_episode_number=number, catalog_title=title,
            catalog_collection=collection,
        ) for number in (1, 2)]
        session.add(collection)
        session.commit()
        collection_id, title_id = collection.id, title.id
        video_ids = [video.id for video in videos]

    definitions_json = (
        '[{"title_id": %d, "local_title": "Season 1", '
        '"part_type_manual": "season", "season_number_manual": 1, '
        '"season_label_manual": "S1", "video_ids": [%s]}]'
        % (title_id, ", ".join(str(video_id) for video_id in video_ids))
    )
    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }

    response = endpoints["/hierarchy-review/{collection_id}/apply"](
        collection_id, definitions_json=definitions_json, confirm_conflicts=False,
    )

    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    assert target.path == f"/hierarchy-review/{collection_id}"
    assert target.fragment == "operation-result"
    message = parse_qs(target.query)["message"][0]
    assert message == "Ruční rozdělení bylo úspěšně aplikováno."

    rendered = endpoints["/hierarchy-review/{collection_id}"](
        web_request(web_app, target.path), collection_id, message=message,
    ).body.decode()
    assert (
        '<div class="notice success" id="operation-result">'
        "Ruční rozdělení bylo úspěšně aplikováno.</div>"
    ) in rendered
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        title = session.get(CatalogTitle, title_id)
        assert collection.hierarchy_status == "verified"
        assert title.hierarchy_manual_override is True
        assert title.effective_part_type == "season"
        assert title.effective_season_number == 1


def test_isekai_quartet_movie_can_be_marked_as_film_from_hierarchy_review(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'isekai-quartet.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    movie_file = tmp_path / "Isekai Quartet Movie - Another World Movie.mkv"
    movie_file.write_bytes(b"unchanged movie")
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Isekai Quartet", normalized_local_title="isekai quartet",
            relative_root_path="Anime/Isekai Quartet", hierarchy_status="review_required",
        )
        movie = CatalogTitle(
            collection=collection, local_title="Isekai Quartet - Another World Movie",
            normalized_local_title="isekai quartet another world movie",
            relative_root_path=(
                "Anime/Isekai Quartet/Isekai Quartet - Another World Movie"
            ),
            part_type="title", sort_order=0,
            metadata_record=TitleMetadata(
                display_title="Isekai Quartet Movie: Another World",
                title_romaji="Isekai Quartet Movie: Another World",
                release_year=2022, format="MOVIE",
            ),
        )
        season_one = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Isekai Quartet/Season 1",
            part_type="season", season_number=1, season_label="S1", sort_order=1,
        )
        season_two = CatalogTitle(
            collection=collection, local_title="Season 2",
            normalized_local_title="season 2",
            relative_root_path="Anime/Isekai Quartet/Season 2",
            part_type="season", season_number=2, season_label="S2", sort_order=2,
        )
        movie_video = Video(
            relative_path=(
                "Anime/Isekai Quartet/Isekai Quartet - Another World Movie/"
                "Isekai Quartet Movie - Another World Movie.mkv"
            ),
            root_folder="Anime",
            filename="Isekai Quartet Movie - Another World Movie.mkv",
            size=len(b"unchanged movie"), mtime_ns=123,
            catalog_title=movie, catalog_collection=collection,
        )
        for number, title in ((1, season_one), (2, season_two)):
            Video(
                relative_path=f"{title.relative_root_path}/Episode 01.mkv",
                root_folder="Anime", filename="Episode 01.mkv", size=1,
                mtime_ns=number, season_episode_number=1,
                catalog_title=title, catalog_collection=collection,
            )
        session.add(collection)
        session.commit()
        collection_id, movie_id = collection.id, movie.id
        season_ids = (season_one.id, season_two.id)
        video_id = movie_video.id
        engine = session.get_bind()

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None)
        == "/collections/{collection_id}/titles/{catalog_title_id}/hierarchy"
    )
    response = endpoint(
        collection_id, movie_id,
        season_number_manual="", season_label_manual="",
        part_number_manual="",
        part_type_manual="film", sort_order_manual="",
        hierarchy_verified=True, filter_name="all", q="", sort="", direction="",
        return_to="hierarchy_review",
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/hierarchy-review/{collection_id}#title-{movie_id}"
    )
    with web_app.state.sessions() as session:
        movie = session.get(CatalogTitle, movie_id)
        movie_video = session.get(Video, video_id)
        seasons = [session.get(CatalogTitle, title_id) for title_id in season_ids]
        assert movie.effective_part_type == "film"
        assert movie.part_type_manual == "film"
        assert movie.hierarchy_manual_override is True
        assert movie.hierarchy_verified_at is not None
        assert movie_video.content_type_manual is None
        assert movie_video.catalog_collection_id == collection_id
        assert movie_video.catalog_title_id == movie_id
        assert (
            movie_video.local_episode_number,
            movie_video.season_episode_number,
            movie_video.absolute_episode_number,
            movie_video.external_episode_number,
            movie_video.episode_number_manual_override,
        ) == (None, None, None, None, None)
        assert is_film_video(movie_video) is True
        assert video_matches_filter(movie_video, "films") is True
        assert [(title.effective_part_type, title.effective_season_number)
                for title in seasons] == [("season", 1), ("season", 2)]
        assert [title.videos[0].season_episode_number for title in seasons] == [1, 1]
        assert movie_video.filename == "Isekai Quartet Movie - Another World Movie.mkv"
        assert movie_video.relative_path.endswith(
            "/Isekai Quartet Movie - Another World Movie.mkv"
        )
        assert movie_video.mtime_ns == 123
        assert movie.metadata_record.display_title == (
            "Isekai Quartet Movie: Another World"
        )
        assert movie.metadata_record.release_year == 2022

    migrate_schema(engine)

    with web_app.state.sessions() as session:
        movie = session.get(CatalogTitle, movie_id)
        movie_video = session.get(Video, video_id)
        seasons = [session.get(CatalogTitle, title_id) for title_id in season_ids]
        assert movie.effective_part_type == "film"
        assert movie.hierarchy_manual_override is True
        assert movie_video.content_type_manual is None
        assert movie_video.catalog_collection_id == collection_id
        assert movie_video.catalog_title_id == movie_id
        assert movie_video.season_episode_number is None
        assert is_film_video(movie_video) is True
        assert [(title.effective_part_type, title.effective_season_number)
                for title in seasons] == [("season", 1), ("season", 2)]
        assert [title.videos[0].season_episode_number for title in seasons] == [1, 1]
        assert movie_video.filename == "Isekai Quartet Movie - Another World Movie.mkv"
        assert movie.metadata_record.title_romaji == (
            "Isekai Quartet Movie: Another World"
        )
    assert movie_file.read_bytes() == b"unchanged movie"


def _render_hierarchy_review(
    *, verified=False, filename="Episode 01.mkv",
    automatic_season_number=None,
):
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
        relative_root_path=collection.relative_root_path,
        part_type="season" if automatic_season_number is not None else "title",
        season_number=automatic_season_number,
        season_label=(
            f"S{automatic_season_number}"
            if automatic_season_number is not None else None
        ),
        hierarchy_verified_at=utc_now() if verified else None,
        hierarchy_manual_override=verified,
        part_type_manual="season" if verified else None,
        season_number_manual=1 if verified else None,
        season_label_manual="S1" if verified else None,
    )
    detection = detect_episode_number(filename)
    is_standard = detection.is_standard
    video = Video(
        id=1, relative_path=f"{collection.relative_root_path}/{filename}",
        root_folder="Anime", filename=filename, size=1, mtime_ns=1,
        season_episode_number=1 if is_standard else None, catalog_title=title,
        catalog_collection=collection,
    )
    summary = summarize_title_numbering([video], title)
    groups = {
        "standard": [{"video": video, "detection": detection}] if is_standard else [],
        "supplemental": [],
        "nonstandard": (
            [{"video": video, "detection": detection}]
            if detection.is_nonstandard else []
        ),
        "unknown": (
            [{"video": video, "detection": detection}]
            if detection.kind == "unknown" else []
        ),
    }
    suggestion = single_title_confirmation_suggestion(collection)
    return templates.env.get_template("hierarchy_review_detail.html").render(
        request=type("Request", (), {
            "url_for": lambda self, *args, **kwargs: "/static/style.css",
        })(),
        collection=collection, videos=[video], numbering_unknown=0,
        nonstandard_videos=(groups["nonstandard"]),
        unassigned_videos={"standard": [], "nonstandard": [], "unknown": []},
        message=None, error=None,
        part_confirmation_suggestion=suggestion,
        part_confirmation_summary=summary if suggestion is not None else None,
        title_numbering=[{
            "title": title, "summary": summary, "videos": groups,
            "metadata_linked": False, "can_delete": False,
        }],
        metadata_status_labels={"unlinked": "Bez metadat"},
        simple_rows=simple_definition_rows(collection), definitions_json="[]",
        external_search_candidates=[], external_candidates=[],
        preview=None, preview_rows=[],
        assignment_recommendations=supplementary_assignment_recommendations([video]),
        assignment_recommendation_by_video={
            video.id: recommendation
            for recommendation in supplementary_assignment_recommendations([video])
        },
        supplementary_suggestions=[], supplementary_suggestion_by_video={},
        duplicate_candidate_video_ids=set(), confirmed_duplicate_video_ids=set(),
        available_collections=[], duplicate_video_details={},
    )


def test_hierarchy_review_shows_editable_part_confirmation_and_human_friendly_form():
    rendered = _render_hierarchy_review()

    assert "Ručně potvrdit typ jediné části" in rendered
    assert "Doporučené zařazení: Season 1 (S1)" in rendered
    assert "1/1 standardních epizod · E1–E1 · unknown 0 · nestandardní 0" in rendered
    assert 'name="season_number_manual" value="1"' in rendered
    assert 'name="season_label_manual" value=""' in rendered
    assert "Jediný CatalogTitle nemusí být Season 1" in rendered
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
    assert rendered.index("Doporučené zařazení") < rendered.index(
        'name="part_type_manual"'
    ) < rendered.index("Jediný CatalogTitle nemusí být Season 1")


def test_hierarchy_review_displays_season_two_from_existing_proposal():
    rendered = _render_hierarchy_review(automatic_season_number=2)

    assert "Doporučené zařazení: Season 2 (S2)" in rendered
    assert 'name="season_number_manual" value="2"' in rendered
    assert 'name="season_label_manual" value="S2"' in rendered


def test_collection_and_title_verification_texts_are_distinct():
    rendered = _render_hierarchy_review(verified=True)

    assert "Hierarchie ověřena" in rendered
    assert "Zařazení ověřeno" in rendered
    assert "Ručně potvrdit typ jediné části" not in rendered
    assert "Doporučené zařazení:" not in rendered


def test_historical_incomplete_part_snapshot_is_not_shown_as_verified(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'historical-part-snapshot.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    historical_timestamp = utc_now()
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Genjitsu Shugi Yuusha no Oukoku Saikenki (L21-Z22)",
            normalized_local_title=(
                "genjitsu shugi yuusha no oukoku saikenki l21 z22"
            ),
            relative_root_path=(
                "Anime/Genjitsu Shugi Yuusha no Oukoku Saikenki (L21-Z22)"
            ),
            hierarchy_status="verified",
            hierarchy_verified_at=historical_timestamp,
        )
        titles = []
        for ordinal, raw_type in ((1, "title"), (2, "migration_review")):
            title = CatalogTitle(
                collection=collection,
                local_title=f"Part {ordinal}",
                normalized_local_title=f"part {ordinal}",
                relative_root_path=(
                    f"{collection.relative_root_path}/Part {ordinal}"
                ),
                part_type=raw_type,
                part_number=ordinal,
                part_type_manual="part",
                season_number_manual=1,
                season_label_manual=f"Part {ordinal}",
                part_number_manual=None,
                sort_order_manual=ordinal,
                hierarchy_manual_override=True,
                hierarchy_verified_at=historical_timestamp,
            )
            Video(
                relative_path=(
                    f"{title.relative_root_path}/Genjitsu - 01.mkv"
                ),
                root_folder="Anime", filename="Genjitsu - 01.mkv",
                size=1, mtime_ns=ordinal,
                catalog_title=title, catalog_collection=collection,
            )
            titles.append(title)
        session.add(collection)
        refresh_collection_state(collection)
        session.commit()
        collection_id = collection.id
        title_ids = [title.id for title in titles]

    endpoints = {
        route.path: route.endpoint
        for route in web_app.routes if hasattr(route, "endpoint")
    }
    review_html = endpoints["/hierarchy-review/{collection_id}"](
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    assert "stav <strong>review_required</strong>" in review_html
    assert "Část typu Part nemá bezpečně určené číslo Part." in review_html
    for ordinal, title_id in zip((1, 2), title_ids, strict=True):
        card = review_html.split(
            f'id="title-{title_id}"', 1,
        )[1].split("</article>", 1)[0]
        assert f"S1 · Part {ordinal}" in card
        assert "Historické ruční zařazení není úplné." in card
        assert "Pro typ Part potvrďte číslo Part." in card
        assert f"Automaticky rozpoznané číslo Part: {ordinal}." in card
        assert '<span class="verified">Zařazení ověřeno</span>' not in card
        assert not re.search(
            r'name="hierarchy_verified"[^>]*\schecked(?:\s|>)', card,
        )
        assert 'name="part_number_manual" value=""' in card

    title_detail_html = endpoints["/titles/{catalog_title_id}"](
        web_request(web_app, f"/titles/{title_ids[1]}"), title_ids[1],
    ).body.decode()
    assert "S1 · Part 2" in title_detail_html

    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        assert collection.hierarchy_status == "review_required"
        for ordinal, title_id in zip((1, 2), title_ids, strict=True):
            title = session.get(CatalogTitle, title_id)
            assert title.part_number == ordinal
            assert title.part_number_manual is None
            assert title.hierarchy_manual_override is True
            assert title.hierarchy_verified_at == historical_timestamp.replace(
                tzinfo=None,
            )


def test_choyoyu_recommendation_is_read_only_and_uses_existing_summary(tmp_path):
    search_title = (
        "Choujin Koukousei-tachi wa Isekai demo Yoyuu de Ikinuku you desu!"
    )
    local_title = f"{search_title} P19"
    settings = Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'choyoyu-recommendation.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    )
    web_app = create_app(settings)
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title=local_title,
            normalized_local_title=(
                "choujin koukousei tachi wa isekai demo yoyuu de ikinuku you desu p19"
            ),
            relative_root_path=f"Anime/{local_title}",
            hierarchy_status="review_required",
            hierarchy_note=PERIOD_HINT_REVIEW_REASON,
        )
        title = CatalogTitle(
            collection=collection, local_title=local_title,
            normalized_local_title=collection.normalized_local_title,
            relative_root_path=collection.relative_root_path, part_type="title",
            metadata_record=TitleMetadata(
                display_title="Choyoyu", format="TV_SHORT", episode_count=12,
            ),
        )
        for number in range(1, 13):
            Video(
                relative_path=f"Anime/{local_title}/Choyoyu - {number:02}.mkv",
                root_folder="Anime", filename=f"Choyoyu - {number:02}.mkv",
                size=1, mtime_ns=1, local_episode_number=number,
                season_episode_number=number, absolute_episode_number=number,
                catalog_title=title, catalog_collection=collection,
            )
        session.add(collection)
        refresh_collection_state(collection)
        session.commit()
        collection_id, title_id = collection.id, title.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )
    rendered = endpoint(
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    assert "Doporučené zařazení: Season 1 (S1)" in rendered
    assert (
        "TV / TV_SHORT · 12/12 standardních epizod · E1–E12 · "
        "unknown 0 · nestandardní 0"
    ) in rendered
    assert "Formulář z něj vytvoří autoritativní ruční snapshot" in rendered
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        title = session.get(CatalogTitle, title_id)
        assert title.part_type_manual is None
        assert title.season_number_manual is None
        assert title.season_label_manual is None
        assert title.hierarchy_manual_override is False
        assert title.hierarchy_verified_at is None
        assert (title.part_type, title.season_number, title.season_label) == (
            "season", 1, "S1",
        )
        assert collection.hierarchy_status == "automatic"
        assert collection.hierarchy_verified_at is None

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    response = endpoints["/hierarchy-review/{collection_id}/confirm-part"](
        collection_id, part_type_manual="season", season_number_manual="1",
        season_label_manual="S1", part_number_manual="", confirm_part=True,
    )
    assert response.status_code == 303

    detail = endpoints["/titles/{catalog_title_id}"](
        web_request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()
    assert (
        'name="metadata_query" maxlength="200" required '
        f'value="{search_title}"'
    ) in detail
    assert f'value="{search_title} Season 1"' not in detail
    assert f'value="{search_title} S1"' not in detail
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        title = session.get(CatalogTitle, title_id)
        assert title.part_type_manual == "season"
        assert title.season_number_manual == 1
        assert title.season_label_manual == "S1"
        assert title.hierarchy_manual_override is True
        assert title.metadata_record.display_title == "Choyoyu"
        assert title.local_title == local_title
        assert collection.local_title == local_title


def test_season_two_confirmation_clears_period_hint_reason_and_renders_verified(
    tmp_path,
):
    settings = Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'season-confirmation.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    )
    web_app = create_app(settings)
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Asobi Asobase (L18)",
            normalized_local_title="asobi asobase l18",
            relative_root_path="Anime/Asobi Asobase (L18)",
            local_period_hint="L18", hierarchy_status="review_required",
            hierarchy_note=PERIOD_HINT_REVIEW_REASON,
        )
        title = CatalogTitle(
            collection=collection, local_title="Asobi Asobase (L18)",
            normalized_local_title="asobi asobase l18",
            relative_root_path="Anime/Asobi Asobase (L18)",
            metadata_status="linked_manual",
            metadata_record=TitleMetadata(
                display_title="Asobi Asobase", title_romaji="Asobi Asobase",
                metadata_provider="anilist", metadata_external_id="37171",
            ),
        )
        title.external_links.append(ExternalTitleLink(
            provider="anilist", external_id="37171", match_method="manual_search",
            is_primary=True, is_manual=True,
        ))
        for number in range(1, 13):
            Video(
                relative_path=(
                    f"Anime/Asobi Asobase (L18)/Asobi Asobase - {number:02}.mkv"
                ),
                root_folder="Anime", filename=f"Asobi Asobase - {number:02}.mkv",
                size=1, mtime_ns=1, local_episode_number=number,
                season_episode_number=number, absolute_episode_number=number,
                catalog_title=title, catalog_collection=collection,
            )
        session.add(collection)
        session.commit()
        collection_id, title_id = collection.id, title.id

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    proposal_rendered = endpoints["/hierarchy-review/{collection_id}"](
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()
    assert "Doporučené zařazení: Season 1 (S1)" in proposal_rendered
    response = endpoints["/hierarchy-review/{collection_id}/confirm-part"](
        collection_id, part_type_manual="season", season_number_manual="2",
        season_label_manual="", part_number_manual="", confirm_part=True,
    )

    assert response.status_code == 303
    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        title = session.get(CatalogTitle, title_id)
        assert collection.hierarchy_status == "verified"
        assert collection.hierarchy_note is None
        assert collection.hierarchy_verified_at is not None
        assert title.season_number_manual == 2
        assert title.season_label_manual == "S2"
        assert title.part_type_manual == "season"
        assert title.hierarchy_verified_at is not None
        assert title.metadata_record.title_romaji == "Asobi Asobase"
        assert title.local_title == "Asobi Asobase (L18)"
        assert collection.local_title == "Asobi Asobase (L18)"

    detail = endpoints["/titles/{catalog_title_id}"](
        web_request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()
    assert 'name="metadata_query" maxlength="200" required value="Asobi Asobase"' in detail
    assert 'value="Asobi Asobase Season 2"' not in detail
    assert 'value="Asobi Asobase S2"' not in detail

    rendered = endpoints["/hierarchy-review/{collection_id}"](
        web_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()

    assert "stav <strong>verified</strong> · Hierarchie ověřena" in rendered
    assert "Interní suffix: L18 · videí: 12" in rendered
    assert PERIOD_HINT_REVIEW_REASON not in rendered
    assert '<option value="verified" selected>Hierarchie ověřena</option>' in rendered
    assert "Lokální část: <strong>Asobi Asobase (L18)</strong>" in rendered
    assert "strukturální identita: <strong>S2</strong>" in rendered
    assert "typ: <strong>season</strong>" in rendered
    assert "<dt>Fyzických videí</dt><dd>12</dd>" in rendered
    assert "<dt>Logických standardních epizod</dt><dd>12</dd>" in rendered
    assert "<dt>Očíslováno</dt><dd>12/12</dd>" in rendered
    assert "<dt>Rozsah</dt><dd>E1–E12</dd>" in rendered
    assert "<dt>Unknown</dt><dd>0</dd>" in rendered
    assert "<dt>Nestandardní</dt><dd>0</dd>" in rendered
    assert "<dt>Zařazený doplňkový obsah</dt><dd>0</dd>" in rendered
    assert "Ručně zařazený doplněk" not in rendered
    assert "Metadata propojena" in rendered
    assert "Číslování vyřešeno" in rendered


def test_hierarchy_review_marks_zero_and_offers_logical_separation():
    rendered = _render_hierarchy_review(filename="Title 00.mp4")

    assert "Nestandardní obsah (1)" in rendered
    assert "Nestandardní epizoda: 00" in rendered
    assert "Vyžaduje zařazení" in rendered
    assert "Oddělit do nové části" in rendered
    assert "relative_path" in rendered
    assert "Externí metadata jsou volitelná" in rendered


def test_hierarchy_review_distinguishes_fractional_and_unknown():
    fractional = _render_hierarchy_review(filename="Title 14.5.mkv")
    unknown = _render_hierarchy_review(filename="Opening.mkv")

    assert "Nestandardní epizoda: 14.5" in fractional
    assert "Nestandardní číslování – vyžaduje kontrolu" in fractional
    assert "Unknown (1)" not in fractional
    assert "Unknown (1)" in unknown
    assert "Parser bezpečně neurčil číslo epizody" in unknown
    assert "Nestandardní epizoda:" not in unknown


def test_hierarchy_review_offers_assignment_for_standard_videos_and_bulk_preview():
    rendered = _render_hierarchy_review(filename="Title 01.mkv")

    assert "Správa zařazení" in rendered
    assert "Ponechat v této části a klasifikovat jako" in rendered
    assert "Přesunout do existující části" in rendered
    assert "Oddělit do nové části" in rendered
    assert "Hromadné číslování vybraných videí" in rendered
    assert 'name="video_ids" value="1"' in rendered


def test_bulk_numbering_preview_template_requires_explicit_confirmation():
    row = type("Row", (), {
        "video_id": 1, "filename": "OVA P1.mkv", "current_episode": None,
        "proposed_episode": 1, "manual_conflict": False,
    })()
    rendered = templates.env.get_template("bulk_numbering_preview.html").render(
        request=type("Request", (), {
            "url_for": lambda self, *args, **kwargs: "/static/style.css",
        })(),
        collection_id=1, collection_name="Arifureta", rows=[row], start_episode=1,
    )

    assert "OVA P1.mkv" in rendered
    assert "E1" in rendered
    assert 'name="confirm_apply"' in rendered


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
    assert 'action="/videos/1/media-part"' in rendered
    assert "Část média" in rendered
    assert 'action="/videos/1/hardsub"' in rendered
    assert (
        video.local_episode_number, video.season_episode_number,
        video.absolute_episode_number, video.external_episode_number,
        video.catalog_title_id, title.season_number, title.season_label,
    ) == values_before_render


def test_media_part_web_workflow_is_separate_from_hierarchy_and_metadata(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'media-part-web.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Movie Collection", normalized_local_title="movie collection",
            relative_root_path="Anime/Movie Collection",
            hierarchy_status="automatic", hierarchy_note=None,
        )
        title = CatalogTitle(
            collection=collection, local_title="Movie Part 2",
            normalized_local_title="movie part 2",
            relative_root_path="Anime/Movie Collection/Movie Part 2",
            part_type="part", season_number=1, part_number=2,
            season_label="S1", metadata_status="linked_manual",
            metadata_record=TitleMetadata(
                display_title="Movie Metadata", metadata_provider="anilist",
                metadata_external_id="123",
            ),
        )
        first = Video(
            relative_path=f"{title.relative_root_path}/Segment A.mkv",
            root_folder="Anime", filename="Segment A.mkv", size=1, mtime_ns=1,
            season_episode_number=4, catalog_title=title,
            catalog_collection=collection,
        )
        second = Video(
            relative_path=f"{title.relative_root_path}/Segment B.mkv",
            root_folder="Anime", filename="Segment B.mkv", size=1, mtime_ns=2,
            season_episode_number=4, media_part_number=2,
            catalog_title=title, catalog_collection=collection,
        )
        duplicate = Video(
            relative_path=f"{title.relative_root_path}/Segment A copy.mkv",
            root_folder="Anime", filename="Segment A copy.mkv", size=1, mtime_ns=3,
            season_episode_number=4, media_part_number=1,
            catalog_title=title, catalog_collection=collection,
            duplicate_of=first,
        )
        session.add(collection)
        session.commit()
        collection_id, title_id = collection.id, title.id
        first_id, second_id, duplicate_id = first.id, second.id, duplicate.id
        metadata_identity = (
            title.metadata_record.metadata_provider,
            title.metadata_record.metadata_external_id,
            title.metadata_record.display_title,
        )
        hierarchy_before = (
            collection.hierarchy_status, collection.hierarchy_note,
            title.part_type, title.part_number, title.part_number_manual,
            title.season_number, title.season_number_manual, title.numbering_mode,
            first.season_episode_number, second.season_episode_number,
        )

    endpoints = {
        route.path: route.endpoint
        for route in web_app.routes if hasattr(route, "endpoint")
    }
    media_endpoint = endpoints["/videos/{video_id}/media-part"]

    response = media_endpoint(
        first_id, media_part_number="1", filter_name="all", q="", sort="",
        direction="", detail_sort="", detail_direction="",
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith(f"#video-{first_id}")

    detail_html = endpoints["/titles/{catalog_title_id}"](
        web_request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()
    collection_html = endpoints["/collections/{collection_id}"](
        web_request(web_app, f"/collections/{collection_id}"), collection_id,
        filter_name="all", q="", sort=None, direction=None,
    ).body.decode()
    assert "S1 · Part 2" in detail_html
    assert "Fyzické členění: <strong>2 části média</strong>" in detail_html
    assert "Fyzické členění: 2 části média" in collection_html
    assert "Část média 1/2" in detail_html
    assert "Část média 2/2" in detail_html
    assert "MP1" not in detail_html
    assert f'action="/videos/{first_id}/media-part"' in detail_html
    assert f'action="/videos/{second_id}/media-part"' in detail_html
    assert f'action="/videos/{duplicate_id}/media-part"' in detail_html
    assert "více aktivních primárních videí" not in detail_html
    duplicate_block = detail_html.split("Segment A copy.mkv", 1)[1]
    assert "Část média 1/2" in duplicate_block

    for invalid in ("0", "-1", "not-a-number"):
        with pytest.raises(HTTPException) as exc_info:
            media_endpoint(
                first_id, media_part_number=invalid, filter_name="all", q="",
                sort="", direction="", detail_sort="", detail_direction="",
            )
        assert exc_info.value.status_code == 400
        assert "kladné celé číslo" in exc_info.value.detail

    response = media_endpoint(
        first_id, media_part_number="2", filter_name="all", q="", sort="",
        direction="", detail_sort="", detail_direction="",
    )
    assert response.status_code == 303
    conflict_html = endpoints["/titles/{catalog_title_id}"](
        web_request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()
    assert "Číslo části média 2 používá více aktivních primárních videí" in conflict_html

    response = media_endpoint(
        first_id, media_part_number="", filter_name="all", q="", sort="",
        direction="", detail_sort="", detail_direction="",
    )
    assert response.status_code == 303
    cleared_html = endpoints["/titles/{catalog_title_id}"](
        web_request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()
    assert "Část média 2/2" not in cleared_html
    assert "Část média 2" in cleared_html

    with web_app.state.sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        title = session.get(CatalogTitle, title_id)
        first = session.get(Video, first_id)
        second = session.get(Video, second_id)
        duplicate = session.get(Video, duplicate_id)
        assert first.media_part_number is None
        assert second.media_part_number == 2
        assert duplicate.media_part_number == 1
        assert (
            title.metadata_record.metadata_provider,
            title.metadata_record.metadata_external_id,
            title.metadata_record.display_title,
        ) == metadata_identity
        assert (
            collection.hierarchy_status, collection.hierarchy_note,
            title.part_type, title.part_number, title.part_number_manual,
            title.season_number, title.season_number_manual, title.numbering_mode,
            first.season_episode_number, second.season_episode_number,
        ) == hierarchy_before


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
        request=request, collections=[], folders=[(".", stats)], totals={
            "anime_titles": 0, "episodes": 0, "films": 0, "bonus": 2,
            "only_cs": 0, "only_sk": 0, "both_cs_sk": 0, "missing": 2,
            "unknown": 0, "total": 2,
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


def test_homepage_collection_name_uses_metadata_preference_and_local_fallback(
    tmp_path,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'homepage-display-title.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        metadata_collection = CatalogCollection(
            local_title="Fyzický název (P21)",
            normalized_local_title="fyzicky nazev p21",
            relative_root_path="Anime/Fyzický název (P21)",
        )
        metadata_title = CatalogTitle(
            collection=metadata_collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Fyzický název (P21)/Season 1",
            part_type="part",
            part_number=1,
            metadata_status="linked_manual",
            metadata_record=TitleMetadata(
                display_title="Metadata English - Part 1",
                title_romaji="Metadata Romaji - Part 1",
                title_english="Metadata English - Part 1",
                title_native="メタデータ原題",
            ),
        )
        metadata_video = Video(
            relative_path="Anime/Fyzický název (P21)/Season 1/01.mkv",
            root_folder="Anime", filename="01.mkv", size=1, mtime_ns=1,
            file_type="episode", catalog_collection=metadata_collection,
            catalog_title=metadata_title,
        )
        fallback_collection = CatalogCollection(
            local_title="Bez metadat (L21)",
            normalized_local_title="bez metadat l21",
            relative_root_path="Anime/Bez metadat (L21)",
        )
        fallback_title = CatalogTitle(
            collection=fallback_collection,
            local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Bez metadat (L21)/Season 1",
        )
        fallback_video = Video(
            relative_path="Anime/Bez metadat (L21)/Season 1/01.mkv",
            root_folder="Anime", filename="01.mkv", size=1, mtime_ns=1,
            file_type="episode", catalog_collection=fallback_collection,
            catalog_title=fallback_title,
        )
        session.add_all([metadata_video, fallback_video])
        session.commit()
        metadata_title_id = metadata_title.id
        fallback_title_id = fallback_title.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/"
    )

    def render(preference: str | None = None) -> str:
        headers = [] if preference is None else [(
            b"cookie",
            f"{PREFERRED_TITLE_LANGUAGE_COOKIE}={preference}".encode(),
        )]
        request = Request({
            "type": "http", "app": web_app, "method": "GET", "path": "/",
            "root_path": "", "scheme": "http", "query_string": b"",
            "headers": headers, "server": ("testserver", 80),
            "client": ("testclient", 50000),
        })
        page = endpoint(request).body.decode()
        return page.split('class="panel logical-catalog"', 1)[1].split(
            'class="panel physical-folders"', 1
        )[0]

    default_catalog = render()
    assert f'href="/titles/{metadata_title_id}">Metadata Romaji</a>' in default_catalog
    assert f'href="/titles/{fallback_title_id}">Bez metadat (L21)</a>' in default_catalog
    assert ">Fyzický název (P21)</a>" not in default_catalog

    expected_by_preference = {
        "english": "Metadata English",
        "native": "メタデータ原題",
    }
    for preference, expected in expected_by_preference.items():
        catalog = render(preference)
        assert f'href="/titles/{metadata_title_id}">{expected}</a>' in catalog
        assert ">Fyzický název (P21)</a>" not in catalog


def test_homepage_uses_logical_collections_and_simplifies_unambiguous_navigation(
    tmp_path,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'homepage.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())

        def add_collection(
            name, relative_root_path, parts, *, root_video=False,
        ):
            collection = CatalogCollection(
                local_title=name, normalized_local_title=name.casefold(),
                relative_root_path=relative_root_path,
            )
            titles = []
            videos = []
            for index, (part_name, part_type) in enumerate(parts, 1):
                title = CatalogTitle(
                    collection=collection, local_title=part_name,
                    normalized_local_title=part_name.casefold(),
                    relative_root_path=f"{relative_root_path}/title-{index}",
                    part_type=part_type,
                    season_number=index if part_type == "season" else None,
                    season_label=f"S{index}" if part_type == "season" else None,
                    sort_order=index,
                )
                filename = f"{name} {part_name}.mkv"
                video = Video(
                    relative_path=(filename if root_video else f"Anime/{name}/{filename}"),
                    root_folder="." if root_video else "Anime",
                    filename=filename, size=1, mtime_ns=index,
                    file_type="other" if part_type in {"film", "ova", "special"} else "episode",
                    catalog_collection=collection, catalog_title=title,
                )
                titles.append(title)
                videos.append(video)
            session.add_all(videos)
            return collection, titles, videos

        single, single_titles, single_videos = add_collection(
            "Single Season", "Anime/Single Season", [("Season 1", "season")]
        )
        film, film_titles, film_videos = add_collection(
            "Standalone Film", "Anime/Standalone Film", [("Film", "film")]
        )
        film_titles[0].part_type = "title"
        film_titles[0].part_type_manual = "film"
        film_titles[0].hierarchy_manual_override = True
        multi, multi_titles, multi_videos = add_collection(
            "Two Seasons", "Anime/Two Seasons",
            [("Season 1", "season"), ("Season 2", "season")],
        )
        mixed, mixed_titles, mixed_videos = add_collection(
            "Series Plus OVA", "Anime/Series Plus OVA",
            [("Season 1", "season"), ("OVA", "ova")],
        )
        root_film, root_film_titles, root_film_videos = add_collection(
            "Virtual Root Film", "@root/101", [("Film", "film")], root_video=True,
        )
        second_root, second_root_titles, second_root_videos = add_collection(
            "Second Root Film", "@root/102", [("Film", "film")], root_video=True,
        )
        legacy_collection = CatalogCollection(
            local_title="Legacy tečka", normalized_local_title="legacy tečka",
            relative_root_path=".",
        )
        legacy_title = CatalogTitle(
            collection=legacy_collection, local_title="Legacy tečka",
            normalized_local_title="legacy tečka", relative_root_path=".",
        )
        legacy_video = Video(
            relative_path="Legacy.mkv", root_folder=".", filename="Legacy.mkv",
            size=1, mtime_ns=1, file_type="other",
            catalog_collection=legacy_collection, catalog_title=legacy_title,
        )
        single_videos[0].internal_subtitles.append(InternalSubtitle(
            stream_index=1, codec="ass", language="cze", normalized_language="cs",
        ))
        film_videos[0].internal_subtitles.append(InternalSubtitle(
            stream_index=1, codec="ass", language="slk", normalized_language="sk",
        ))
        multi_videos[0].internal_subtitles.append(InternalSubtitle(
            stream_index=1, codec="ass", language="cze", normalized_language="cs",
        ))
        multi_videos[0].external_subtitles.append(ExternalSubtitle(
            relative_path="Anime/Two Seasons/Season 1.sk.srt",
            codec="srt", language="slk", normalized_language="sk",
        ))
        multi_videos[1].internal_subtitles.append(InternalSubtitle(
            stream_index=1, codec="ass", language="und",
            normalized_language="unknown",
        ))
        empty_collection = CatalogCollection(
            local_title="Empty orphan", normalized_local_title="empty orphan",
            relative_root_path="Anime/Empty orphan",
        )
        session.add(empty_collection)
        session.add(legacy_video)
        session.commit()
        expected_links = {
            "Single Season": f"/titles/{single_titles[0].id}",
            "Standalone Film": f"/titles/{film_titles[0].id}",
            "Two Seasons": f"/collections/{multi.id}",
            "Series Plus OVA": f"/collections/{mixed.id}",
            "Virtual Root Film": f"/titles/{root_film_titles[0].id}",
            "Second Root Film": f"/titles/{second_root_titles[0].id}",
        }
        hierarchy_before = {
            collection.id: tuple(
                (title.id, title.part_type, title.season_number, title.sort_order)
                for title in collection.titles
            )
            for collection in (single, film, multi, mixed, root_film, second_root)
        }
        physical_before = {
            video.id: (video.relative_path, video.root_folder)
            for video in (*root_film_videos, *second_root_videos)
        }

    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }
    request = Request({
        "type": "http", "app": web_app, "method": "GET", "path": "/",
        "root_path": "", "scheme": "http", "query_string": b"", "headers": [],
        "server": ("testserver", 80), "client": ("testclient", 50000),
    })
    rendered = endpoints["/"](request).body.decode()
    stats_section = rendered.split('<section class="stats">', 1)[1].split(
        "</section>", 1
    )[0]
    logical_section = rendered.split('class="panel logical-catalog"', 1)[1].split(
        'class="panel physical-folders"', 1
    )[0]

    expected_stats = [
        ("Anime titulů", 6),
        ("Běžných epizod", 4),
        ("Filmů", 3),
        ("Bonusových / ostatních videí", 2),
        ("Pouze CZ", 1),
        ("Pouze SK", 1),
        ("CZ i SK", 1),
        ("Bez CZ/SK", 6),
        ("S neznámými titulky", 1),
        ("Celkem videí", 9),
    ]
    for label, count in expected_stats:
        assert f"<strong>{count}</strong><span>{label}</span>" in stats_section
    assert [stats_section.index(label) for label, _ in expected_stats] == sorted(
        stats_section.index(label) for label, _ in expected_stats
    )
    stats_by_label = dict(expected_stats)
    assert (
        stats_by_label["Běžných epizod"]
        + stats_by_label["Filmů"]
        + stats_by_label["Bonusových / ostatních videí"]
        == stats_by_label["Celkem videí"]
    )
    assert (
        'href="/catalog/films"><article><strong>3</strong><span>Filmů</span>'
        in stats_section
    )
    assert stats_section.lstrip().startswith(
        "<article><strong>6</strong><span>Anime titulů</span></article>"
    )
    for filter_name in (
        "episodes", "bonus", "only-cs", "only-sk", "both", "missing", "unknown",
    ):
        assert f'href="/catalog/{filter_name}"' in stats_section

    for name, href in expected_links.items():
        assert f'href="{href}">{name}</a>' in logical_section
    assert logical_section.count("Root Film</a>") == 2
    assert "Legacy tečka" not in logical_section
    assert ROOT_VIDEO_GROUP_LABEL in rendered
    assert 'href="/hierarchy-review"' in logical_section

    with web_app.state.sessions() as session:
        loaded_videos = list(session.scalars(select(Video).options(
            selectinload(Video.catalog_title).selectinload(CatalogTitle.collection),
            selectinload(Video.catalog_collection),
            selectinload(Video.internal_subtitles),
            selectinload(Video.external_subtitles),
        )).all())
        search_groups = build_catalog_results(loaded_videos, "all")
        logical_search_names = {
            group.name for group in search_groups.groups if not group.is_root_group
        }
        assert logical_search_names == set(expected_links)

        collections = session.scalars(select(CatalogCollection).options(
            selectinload(CatalogCollection.titles)
        ).where(CatalogCollection.id.in_(hierarchy_before))).all()
        assert {
            collection.id: tuple(
                (title.id, title.part_type, title.season_number, title.sort_order)
                for title in collection.titles
            )
            for collection in collections
        } == hierarchy_before
        root_videos = [
            session.get(Video, video_id) for video_id in physical_before
        ]
        assert {
            video.id: (video.relative_path, video.root_folder) for video in root_videos
        } == physical_before
        assert session.get(CatalogCollection, root_film.id).relative_root_path == "@root/101"
        assert session.get(CatalogCollection, second_root.id).relative_root_path == "@root/102"

    route_methods = {
        route.path: route.methods for route in web_app.routes if hasattr(route, "methods")
    }
    assert route_methods["/hierarchy-review/{collection_id}/simple-preview"] == {"POST"}
    assert route_methods["/hierarchy-review/{collection_id}/apply"] == {"POST"}


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


def test_created_root_titles_survive_refresh_restart_and_scan(tmp_path):
    paths = [
        tmp_path / "Hotarubi no Mori e.mkv",
        tmp_path / "Koe no Katachi - A Silent Voice.mkv",
        tmp_path / "Legacy Root Movie.mkv",
    ]
    for path in paths:
        path.write_bytes(b"x")
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'root-persistence.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    engine = web_app.state.sessions.kw["bind"]
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        legacy_collection = CatalogCollection(
            local_title="Knihovna", normalized_local_title="knihovna",
            relative_root_path=".",
        )
        legacy_title = CatalogTitle(
            collection=legacy_collection, local_title="Knihovna",
            normalized_local_title="knihovna", relative_root_path=".",
        )
        videos = []
        for path in paths:
            stat = path.stat()
            videos.append(Video(
                relative_path=path.name, root_folder=".", filename=path.name,
                size=stat.st_size, mtime_ns=stat.st_mtime_ns, file_type="other",
            ))
        videos[-1].catalog_collection = legacy_collection
        videos[-1].catalog_title = legacy_title
        session.add_all(videos)
        session.commit()
        first_id, second_id, legacy_id = (video.id for video in videos)

    endpoints = {
        route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")
    }

    def render_root_videos() -> str:
        request = Request({
            "type": "http", "app": web_app, "method": "GET", "path": "/root-videos",
            "root_path": "", "scheme": "http", "query_string": b"", "headers": [],
            "server": ("testserver", 80), "client": ("testclient", 50000),
        })
        return endpoints["/root-videos"](request).body.decode()

    initial_page = render_root_videos()
    assert all(path.name in initial_page for path in paths)
    assert "Technická původní root skupina „.“" in initial_page

    first_response = endpoints["/root-videos/{video_id}/new-title"](
        first_id, display_title="Hotarubi no Mori e", part_type="film"
    )
    assert first_response.status_code == 303
    refreshed_page = render_root_videos()
    assert paths[0].name not in refreshed_page
    assert paths[1].name in refreshed_page

    second_response = endpoints["/root-videos/{video_id}/new-title"](
        second_id, display_title="Koe no Katachi - A Silent Voice", part_type="film"
    )
    assert second_response.status_code == 303

    with web_app.state.sessions() as session:
        stored = [session.get(Video, video_id) for video_id in (first_id, second_id)]
        committed_ids = [
            (video.catalog_collection_id, video.catalog_title_id) for video in stored
        ]
        assert all(collection_id and title_id for collection_id, title_id in committed_ids)
        assert committed_ids[0][0] != committed_ids[1][0]
        assert stored[0].catalog_collection.relative_root_path == f"@root/{first_id}"
        assert stored[0].catalog_title.relative_root_path == f"@root/{first_id}/title"
        assert stored[1].catalog_collection.relative_root_path == f"@root/{second_id}"
        assert stored[1].catalog_title.relative_root_path == f"@root/{second_id}/title"
        assert [(video.relative_path, video.root_folder) for video in stored] == [
            (paths[0].name, "."), (paths[1].name, "."),
        ]

    homepage_request = Request({
        "type": "http", "app": web_app, "method": "GET", "path": "/",
        "root_path": "", "scheme": "http", "query_string": b"", "headers": [],
        "server": ("testserver", 80), "client": ("testclient", 50000),
    })
    homepage = endpoints["/"](homepage_request).body.decode()
    logical_homepage = homepage.split('class="panel logical-catalog"', 1)[1].split(
        'class="panel physical-folders"', 1
    )[0]
    assert f'href="/titles/{committed_ids[0][1]}">Hotarubi no Mori e</a>' in logical_homepage
    assert (
        f'href="/titles/{committed_ids[1][1]}">Koe no Katachi - A Silent Voice</a>'
        in logical_homepage
    )
    assert ROOT_VIDEO_GROUP_LABEL not in logical_homepage

    after_commit_page = render_root_videos()
    assert paths[0].name not in after_commit_page
    assert paths[1].name not in after_commit_page
    assert paths[2].name in after_commit_page

    with web_app.state.sessions() as session:
        loaded_videos = list(session.scalars(select(Video).options(
            selectinload(Video.catalog_title).selectinload(CatalogTitle.collection),
            selectinload(Video.catalog_collection),
            selectinload(Video.internal_subtitles),
            selectinload(Video.external_subtitles),
        ).order_by(Video.id)).all())
        results = build_catalog_results(loaded_videos, "all")
        assert {group.name for group in results.groups} == {
            "Hotarubi no Mori e", "Koe no Katachi - A Silent Voice",
            ROOT_VIDEO_GROUP_LABEL,
        }
        assert sum(group.is_root_group for group in results.groups) == 1

    migrate_schema(engine)
    with web_app.state.sessions() as session:
        restarted = [session.get(Video, video_id) for video_id in (first_id, second_id)]
        assert [
            (video.catalog_collection_id, video.catalog_title_id) for video in restarted
        ] == committed_ids
        legacy_after_restart = session.get(Video, legacy_id)
        assert legacy_after_restart.catalog_collection.relative_root_path == "."
        assert legacy_after_restart.catalog_title.relative_root_path == "."

    with web_app.state.sessions() as session:
        scan_library(session, tmp_path)
    with web_app.state.sessions() as session:
        scanned = [session.get(Video, video_id) for video_id in (first_id, second_id)]
        assert [
            (video.catalog_collection_id, video.catalog_title_id) for video in scanned
        ] == committed_ids
        assert [(video.relative_path, video.root_folder) for video in scanned] == [
            (paths[0].name, "."), (paths[1].name, "."),
        ]
        legacy = session.get(Video, legacy_id)
        assert legacy.catalog_collection_id is None
        assert legacy.catalog_title_id is None
