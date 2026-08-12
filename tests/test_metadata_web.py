from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.catalog import ROOT_VIDEO_GROUP_LABEL, build_catalog_results, detect_episode_number
from app.config import Settings
from app.database import Base
from app.main import (
    PREFERRED_TITLE_LANGUAGE_COOKIE, app, create_app,
    get_preferred_title_language, templates,
)
from app.hierarchy_review import (
    separate_nonstandard_videos, simple_definition_rows, single_season_suggestion,
)
from app.metadata.providers.base import ProviderTitleMetadata
from app.migrations import migrate_schema
from app.models import (
    CatalogCollection, CatalogTitle, ExternalSubtitle, ExternalTitleLink,
    InternalSubtitle, TitleMetadata, Video, utc_now,
)
from app.numbering import summarize_title_numbering
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
    assert paths["/hierarchy-review/{collection_id}/season-one"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/simple-preview"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/separate-nonstandard"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/manage-videos"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/merge-title"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/delete-empty-title"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/numbering-preview"] == {"POST"}
    assert paths["/hierarchy-review/{collection_id}/numbering-apply"] == {"POST"}
    assert paths["/preferences/title-name"] == {"POST"}


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

    response = endpoint(preference="english", return_to="/hierarchy-review")

    assert response.status_code == 303
    assert response.headers["location"] == "/hierarchy-review"
    assert f"{PREFERRED_TITLE_LANGUAGE_COOKIE}=english" in response.headers["set-cookie"]
    assert "Max-Age=31536000" in response.headers["set-cookie"]
    cookie = f"{PREFERRED_TITLE_LANGUAGE_COOKIE}=english".encode()
    for web_app in (first_app, create_app(settings)):
        request = Request({
            "type": "http", "app": web_app, "method": "GET", "path": "/",
            "root_path": "", "scheme": "http", "query_string": b"",
            "headers": [(b"cookie", cookie)], "server": ("testserver", 80),
            "client": ("testclient", 50000),
        })
        assert get_preferred_title_language(request) == "english"


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
    assert "<dt>Standardních epizod</dt><dd>22</dd>" in before
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


def _render_hierarchy_review(*, verified=False, filename="Episode 01.mkv"):
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
    return templates.env.get_template("hierarchy_review_detail.html").render(
        request=type("Request", (), {
            "url_for": lambda self, *args, **kwargs: "/static/style.css",
        })(),
        collection=collection, videos=[video], numbering_unknown=0,
        nonstandard_videos=(groups["nonstandard"]),
        unassigned_videos={"standard": [], "nonstandard": [], "unknown": []},
        message=None, error=None, season_one_suggestion=single_season_suggestion(collection),
        title_numbering=[{
            "title": title, "summary": summary, "videos": groups,
            "metadata_linked": False, "can_delete": False,
        }],
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
        request=request, collections=[], folders=[(".", stats)], totals={
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

        single, single_titles, _ = add_collection(
            "Single Season", "Anime/Single Season", [("Season 1", "season")]
        )
        film, film_titles, _ = add_collection(
            "Standalone Film", "Anime/Standalone Film", [("Film", "film")]
        )
        multi, multi_titles, _ = add_collection(
            "Two Seasons", "Anime/Two Seasons",
            [("Season 1", "season"), ("Season 2", "season")],
        )
        mixed, mixed_titles, _ = add_collection(
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
    logical_section = rendered.split('class="panel logical-catalog"', 1)[1].split(
        'class="panel physical-folders"', 1
    )[0]

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
