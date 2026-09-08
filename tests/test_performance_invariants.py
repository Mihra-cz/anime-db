import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import Settings
from app.catalog import build_catalog_request_index, build_catalog_results
from app.database import Base
from app.main import create_app
from app.models import (
    AudioTrack, CatalogCollection, CatalogTitle, TitleMetadata,
    UnresolvedExternalSubtitle, Video,
)
from app.subtitle_review import build_unresolved_subtitle_rows


def _request(web_app, path: str) -> Request:
    return Request({
        "type": "http",
        "app": web_app,
        "method": "GET",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })


def _semantic_snapshot(engine):
    with engine.connect() as connection:
        return tuple(
            (
                table.name,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        table.select().order_by(*table.primary_key.columns)
                    )
                ),
            )
            for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name)
        )


@pytest.fixture
def performance_app(tmp_path):
    database_path = tmp_path / "performance-invariants.db"
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{database_path}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    engine = web_app.state.sessions.kw["bind"]
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Performance Show",
            normalized_local_title="performance show",
            relative_root_path="Anime/Performance Show",
        )
        title = CatalogTitle(
            collection=collection,
            local_title="Performance Show",
            normalized_local_title="performance show",
            relative_root_path="Anime/Performance Show/Season 1",
            part_type="season",
            season_number=1,
            season_label="S1",
            numbering_mode="local",
        )
        video = Video(
            catalog_collection=collection,
            catalog_title=title,
            relative_path="Anime/Performance Show/Season 1/Show - 01.mkv",
            root_folder="Anime",
            filename="Show - 01.mkv",
            size=1,
            mtime_ns=1,
            file_type="episode",
            local_episode_number=1,
            season_episode_number=1,
        )
        video.audio_tracks.append(AudioTrack(
            stream_index=0,
            codec="aac",
            language="jpn",
        ))
        session.add(collection)
        session.commit()
        ids = {"collection": collection.id, "title": title.id}
    return web_app, ids


@pytest.mark.parametrize(
    ("path", "kwargs"),
    (
        ("/", {"message": None, "error": None, "confirm_deletions": False, "q": ""}),
        ("/catalog/{filter_name}", {
            "filter_name": "all", "q": "", "sort": None, "direction": None,
        }),
        ("/hierarchy-review", {"message": None}),
        ("/metadata-review", {"status": "without"}),
        ("/media-check", {
            "subtitle": "all", "audio": "all", "q": "", "page": 1,
            "message": None,
        }),
    ),
)
def test_stable_get_endpoints_are_semantically_read_only(
    performance_app, path, kwargs,
):
    web_app, _ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == path
    )
    before = _semantic_snapshot(engine)
    writes = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = endpoint(_request(web_app, path), **kwargs)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    assert writes == []
    assert _semantic_snapshot(engine) == before


def test_overlord_content_editor_reuses_classification_and_preserves_raw_evidence(performance_app):
    import asyncio
    from test_metadata_web import post_form_request
    from app.hierarchy_types import VIDEO_CONTENT_TYPE_CHOICES
    from app.catalog import effective_video_content_type

    web_app, ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    with Session(engine) as session:
        title = session.get(CatalogTitle, ids["title"])
        title.part_type = "bonus"
        title.local_title = "Extras – Drama CD"
        original = title.videos[0]
        original.filename = "Overlord Special 05.mkv"
        original.file_type = "special"
        original.local_episode_number = original.season_episode_number = None
        video_id = original.id
        sibling_title = CatalogTitle(
            collection=title.collection, local_title="Extras B", normalized_local_title="extras b",
            relative_root_path="Anime/Performance Show/Extras B", part_type="bonus",
        )
        session.add(Video(
            catalog_title=sibling_title, catalog_collection=title.collection,
            relative_path="Anime/Performance Show/Extras B/drama.mkv",
            filename="drama.mkv", root_folder="Anime", size=1, mtime_ns=1,
            file_type="bonus", content_type_manual="bonus", episode_number_manual_override=6,
        ))
        session.commit()

    endpoints = {route.path: route.endpoint for route in web_app.routes if hasattr(route, "endpoint")}
    path = f"/titles/{ids['title']}"
    def render():
        before = _semantic_snapshot(engine)
        html = endpoints["/titles/{catalog_title_id}"](
            _request(web_app, path), ids["title"],
        ).body.decode()
        assert _semantic_snapshot(engine) == before
        return html.split(f'id="video-{video_id}"', 1)[1].split("</tr>", 1)[0]

    def classify(value):
        action = f"/hierarchy-review/{ids['collection']}/manage-videos"
        response = asyncio.run(endpoints["/hierarchy-review/{collection_id}/manage-videos"](
            post_form_request(web_app, action, [
                ("operation", "classify"), ("video_ids", str(video_id)),
                ("content_type", value), ("return_to", f"{path}#video-{video_id}"),
            ]), ids["collection"],
        ))
        assert response.status_code == 303
        assert response.headers["location"] == f"{path}#video-{video_id}"

    html = render()
    assert "Typ obsahu" in html and "automaticky</option>" in html
    for value, _label in VIDEO_CONTENT_TYPE_CHOICES:
        assert f'<option value="{value}"' in html
    assert "<strong>Special 05</strong>" in html
    classify("bonus")
    html = render()
    assert "Raw typ: special" in html
    assert "<strong>Special 05</strong>" not in html
    assert "<strong>Bonus</strong>" in html and "Bonus ?" in html
    assert "Chybějící supplementary ordinal" in html
    with Session(engine) as session:
        item = session.get(Video, video_id)
        assert effective_video_content_type(item) == "bonus"
        assert item.file_type == "special"
        assert item.episode_number_manual_override is None

    response = endpoints["/videos/{video_id}/episode-number"](
        video_id, manual_episode_number="5", return_to=path,
    )
    assert response.status_code == 303
    html = render()
    assert "<strong>Bonus 05</strong>" in html
    assert "Chybějící supplementary ordinal" not in html
    classify("")
    html = render()
    assert "<strong>Special 05</strong>" in html
    assert "<strong>Bonus 05</strong>" not in html
    with Session(engine) as session:
        item = session.get(Video, video_id)
        assert item.content_type_manual is None
        assert item.file_type == "special"
        assert item.episode_number_manual_override == 5


@pytest.mark.parametrize("route,argument", [
    ("/titles/{catalog_title_id}", "title"),
    ("/hierarchy-review/{collection_id}", "collection"),
    ("/hierarchy-review", None),
])
@pytest.mark.parametrize("structural", [False, True])
def test_collection_wide_identity_review_is_bounded_and_read_only(performance_app, route, argument, structural):
    web_app, ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(r.endpoint for r in web_app.routes if getattr(r, "path", None) == route)
    def measure():
        before = _semantic_snapshot(engine)
        statements = []
        def record(conn, cursor, statement, parameters, context, many):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = (endpoint(_request(web_app, route), ids[argument]) if argument
                        else endpoint(_request(web_app, route)))
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert response.status_code == 200
        assert not any(s.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for s in statements)
        assert _semantic_snapshot(engine) == before
        return len(statements), response.body.decode()

    with Session(engine) as session:
        title = session.get(CatalogTitle, ids["title"])
        title.part_type = "bonus"
        item = title.videos[0]
        item.content_type_manual = "bonus"
        if structural:
            session.add(CatalogTitle(
                collection=title.collection, local_title="S1", normalized_local_title="s1",
                relative_root_path="Anime/Performance Show/Primary 1",
                part_type="season", season_number=1, season_label="S1",
            ))
        session.commit()
    baseline, _ = measure()
    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["collection"])
        for i in range(20):
            title = CatalogTitle(
                collection=collection, local_title=f"Extras {i}", normalized_local_title=f"extras {i}",
                relative_root_path=f"Anime/Performance Show/Extras {i}", part_type="bonus",
                season_number=i + 2 if structural else None,
            )
            session.add(Video(
                catalog_title=title, catalog_collection=collection,
                filename=f"Bonus item {i}.mkv",
                relative_path=f"{title.relative_root_path}/Bonus item {i}.mkv",
                root_folder="Anime", file_type="bonus", size=1, mtime_ns=1,
                content_type_manual="bonus",
                episode_number_manual_override=1 if structural else None,
                media_part_number=1 if structural else None,
            ))
            if structural:
                session.add(CatalogTitle(
                    collection=collection, local_title=f"S{i + 2}", normalized_local_title=f"s{i + 2}",
                    relative_root_path=f"Anime/Performance Show/Primary {i + 2}",
                    part_type="season", season_number=i + 2, season_label=f"S{i + 2}",
                ))
                session.add(Video(
                    catalog_title=title, catalog_collection=collection,
                    filename=f"Bonus item {i} second segment.mkv",
                    relative_path=f"{title.relative_root_path}/Bonus item {i} second segment.mkv",
                    root_folder="Anime", file_type="bonus", size=1, mtime_ns=1,
                    content_type_manual="bonus", episode_number_manual_override=1,
                    media_part_number=2,
                ))
        session.commit()
    expanded, html = measure()
    assert expanded == baseline
    assert ("Chybějící supplementary ordinal" in html) is not structural
    assert "Kolize supplementary ordinalu" not in html


def test_title_detail_count_comparison_is_semantically_read_only(performance_app):
    web_app, ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    with Session(engine) as session:
        title = session.get(CatalogTitle, ids["title"])
        title.metadata_record = TitleMetadata(
            display_title="Performance Show",
            episode_count=1,
            metadata_provider="anilist",
            metadata_external_id="1",
        )
        session.commit()

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/titles/{catalog_title_id}"
    )
    before = _semantic_snapshot(engine)
    writes = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = endpoint(
            _request(web_app, f"/titles/{ids['title']}"), ids["title"],
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    assert "Lokálně: 1 logických položek (shoda)" in response.body.decode()
    assert writes == []
    assert _semantic_snapshot(engine) == before


def test_homepage_query_count_is_bounded_as_video_count_grows(performance_app):
    web_app, ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/"
    )

    def query_count():
        statements = 0

        def increment(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", increment)
        try:
            response = endpoint(
                _request(web_app, "/"),
                message=None,
                error=None,
                confirm_deletions=False,
                q="",
            )
        finally:
            event.remove(engine, "before_cursor_execute", increment)
        assert response.status_code == 200
        return statements

    baseline = query_count()
    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["collection"])
        title = session.get(CatalogTitle, ids["title"])
        session.add_all([
            Video(
                catalog_collection=collection,
                catalog_title=title,
                relative_path=f"Anime/Performance Show/Season 1/Show - {number:03}.mkv",
                root_folder="Anime",
                filename=f"Show - {number:03}.mkv",
                size=number,
                mtime_ns=number,
                file_type="episode",
                local_episode_number=number,
                season_episode_number=number,
            )
            for number in range(2, 202)
        ])
        session.commit()

    assert query_count() == baseline
    assert baseline <= 7


def test_catalog_query_count_is_bounded_as_collection_count_grows(
    performance_app,
):
    web_app, _ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/catalog/{filter_name}"
    )

    def query_count():
        statements = 0

        def increment(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", increment)
        try:
            response = endpoint(
                _request(web_app, "/catalog/all"), "all", q="",
                sort=None, direction=None,
            )
        finally:
            event.remove(engine, "before_cursor_execute", increment)
        assert response.status_code == 200
        return statements

    baseline = query_count()
    with Session(engine) as session:
        for number in range(2, 202):
            collection = CatalogCollection(
                local_title=f"Collection {number:03}",
                normalized_local_title=f"collection {number:03}",
                relative_root_path=f"Anime/Collection {number:03}",
            )
            title = CatalogTitle(
                collection=collection,
                local_title=f"Collection {number:03}",
                normalized_local_title=f"collection {number:03}",
                relative_root_path=f"Anime/Collection {number:03}/Season 1",
                part_type="season", season_number=1,
            )
            session.add(Video(
                catalog_collection=collection, catalog_title=title,
                relative_path=(
                    f"Anime/Collection {number:03}/Season 1/"
                    f"Collection {number:03} - 01.mkv"
                ),
                root_folder="Anime",
                filename=f"Collection {number:03} - 01.mkv",
                size=number, mtime_ns=number, file_type="episode",
                local_episode_number=1, season_episode_number=1,
            ))
        session.commit()

    assert query_count() == baseline
    assert baseline <= 7


def test_collection_detail_query_count_is_bounded_as_title_count_grows(
    performance_app,
):
    web_app, ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/collections/{collection_id}"
    )

    def query_count():
        statements = 0

        def increment(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", increment)
        try:
            response = endpoint(
                _request(
                    web_app, f"/collections/{ids['collection']}"
                ),
                ids["collection"],
            )
        finally:
            event.remove(engine, "before_cursor_execute", increment)
        assert response.status_code == 200
        return statements

    baseline = query_count()
    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["collection"])
        for number in range(2, 102):
            title = CatalogTitle(
                collection=collection,
                local_title=f"Season {number}",
                normalized_local_title=f"season {number}",
                relative_root_path=(
                    f"Anime/Performance Show/Season {number}"
                ),
                part_type="season", season_number=number,
                season_label=f"S{number}", numbering_mode="local",
            )
            session.add(Video(
                catalog_collection=collection, catalog_title=title,
                relative_path=(
                    f"Anime/Performance Show/Season {number}/Show - 01.mkv"
                ),
                root_folder="Anime", filename="Show - 01.mkv",
                size=number, mtime_ns=number, file_type="episode",
                local_episode_number=1, season_episode_number=1,
            ))
        session.commit()

    assert query_count() == baseline
    assert baseline <= 24


def test_hierarchy_overview_query_count_is_bounded_as_supplementary_videos_grow(
    performance_app,
):
    web_app, ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review"
    )

    def query_count():
        statements = 0

        def increment(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", increment)
        try:
            response = endpoint(
                _request(web_app, "/hierarchy-review"), message=None,
            )
        finally:
            event.remove(engine, "before_cursor_execute", increment)
        assert response.status_code == 200
        return statements

    baseline = query_count()
    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["collection"])
        title = session.get(CatalogTitle, ids["title"])
        session.add_all([
            Video(
                catalog_collection=collection,
                catalog_title=title,
                relative_path=f"Anime/Performance Show/Season 1/NCOP{number:03}.mkv",
                root_folder="Anime",
                filename=f"NCOP{number:03}.mkv",
                size=number,
                mtime_ns=number,
                file_type="ncop",
            )
            for number in range(1, 201)
        ])
        session.commit()

    assert query_count() == baseline
    assert baseline <= 7


def test_hierarchy_overview_query_count_is_bounded_as_collection_count_grows(
    performance_app,
):
    web_app, _ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review"
    )

    def query_count():
        statements = 0

        def increment(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", increment)
        try:
            response = endpoint(
                _request(web_app, "/hierarchy-review"), message=None,
            )
        finally:
            event.remove(engine, "before_cursor_execute", increment)
        assert response.status_code == 200
        return statements

    baseline = query_count()
    with Session(engine) as session:
        for number in range(1, 101):
            collection = CatalogCollection(
                local_title=f"Scale Show {number:03}",
                normalized_local_title=f"scale show {number:03}",
                relative_root_path=f"Anime/Scale Show {number:03}",
            )
            title = CatalogTitle(
                collection=collection,
                local_title=collection.local_title,
                normalized_local_title=collection.normalized_local_title,
                relative_root_path=f"{collection.relative_root_path}/Season 1",
                part_type="season",
                season_number=1,
                season_label="S1",
            )
            Video(
                catalog_collection=collection,
                catalog_title=title,
                relative_path=f"{title.relative_root_path}/Episode 01.mkv",
                root_folder="Anime",
                filename="Episode 01.mkv",
                size=number,
                mtime_ns=number,
                file_type="episode",
                local_episode_number=1,
                season_episode_number=1,
            )
            session.add(collection)
        session.commit()

    assert query_count() == baseline
    assert baseline <= 7


def test_hierarchy_gets_with_supplementary_review_are_semantically_read_only(
    performance_app,
):
    web_app, ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["collection"])
        title = session.get(CatalogTitle, ids["title"])
        session.add_all([
            Video(
                catalog_collection=collection,
                catalog_title=title,
                relative_path=f"Anime/Performance Show/Season 1/{filename}",
                root_folder="Anime",
                filename=filename,
                size=2,
                mtime_ns=2,
                file_type="ncop",
            )
            for filename in ("NCOP.mkv", "NCOP clean.mkv")
        ])
        session.commit()

    before = _semantic_snapshot(engine)
    writes = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    event.listen(engine, "before_cursor_execute", record)
    try:
        overview = endpoints["/hierarchy-review"](
            _request(web_app, "/hierarchy-review"), message=None,
        )
        detail = endpoints["/hierarchy-review/{collection_id}"](
            _request(web_app, f'/hierarchy-review/{ids["collection"]}'),
            ids["collection"], message=None,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert overview.status_code == detail.status_code == 200
    assert "Chybějící supplementary ordinal" in overview.body.decode()
    assert "Supplementary ordinal · vyžaduje kontrolu" in detail.body.decode()
    assert writes == []
    assert _semantic_snapshot(engine) == before


def test_hierarchy_detail_query_count_is_bounded_as_video_count_grows(
    performance_app,
):
    web_app, ids = performance_app
    engine = web_app.state.sessions.kw["bind"]
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )

    def query_count():
        statements = 0

        def increment(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", increment)
        try:
            response = endpoint(
                _request(
                    web_app,
                    f"/hierarchy-review/{ids['collection']}",
                ),
                collection_id=ids["collection"],
                message=None,
            )
        finally:
            event.remove(engine, "before_cursor_execute", increment)
        assert response.status_code == 200
        return statements

    baseline = query_count()
    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["collection"])
        title = session.get(CatalogTitle, ids["title"])
        session.add_all([
            Video(
                catalog_collection=collection,
                catalog_title=title,
                relative_path=f"Anime/Performance Show/Season 1/Review - {number:03}.mkv",
                root_folder="Anime",
                filename=f"Review - {number:03}.mkv",
                size=number,
                mtime_ns=number,
                file_type="episode",
                local_episode_number=number,
                season_episode_number=number,
            )
            for number in range(2, 202)
        ])
        session.commit()

    assert query_count() == baseline
    assert baseline <= 18


def test_catalog_request_parses_each_video_once(monkeypatch):
    import app.catalog as catalog_module
    import app.numbering as numbering_module

    collection = CatalogCollection(
        id=1,
        local_title="Linear",
        normalized_local_title="linear",
        relative_root_path="Anime/Linear",
    )
    title = CatalogTitle(
        id=1,
        collection=collection,
        local_title="Linear",
        normalized_local_title="linear",
        relative_root_path="Anime/Linear/Season 1",
        part_type="season",
        season_number=1,
        season_label="S1",
    )
    videos = [
        Video(
            id=number,
            catalog_collection=collection,
            catalog_title=title,
            relative_path=f"Anime/Linear/Season 1/Linear - {number:03}.mkv",
            root_folder="Anime",
            filename=f"Linear - {number:03}.mkv",
            size=number,
            mtime_ns=number,
            file_type="episode",
            local_episode_number=number,
            season_episode_number=number,
        )
        for number in range(1, 201)
    ]
    original = catalog_module.detect_episode_number
    parser_calls = 0

    def count_parser(filename):
        nonlocal parser_calls
        parser_calls += 1
        return original(filename)

    monkeypatch.setattr(catalog_module, "detect_episode_number", count_parser)
    monkeypatch.setattr(numbering_module, "detect_episode_number", count_parser)
    request_index = build_catalog_request_index(videos)
    results = build_catalog_results(
        videos, "all", request_index=request_index,
    )

    assert results.video_count == len(videos)
    assert parser_calls == len(videos)


def test_unresolved_subtitle_candidates_reuse_path_and_parser_index(monkeypatch):
    import app.subtitle_review as subtitle_review_module

    videos = [
        Video(
            id=number,
            relative_path=(
                f"Anime/Indexed/Part {(number - 1) // 50:02}/"
                f"Indexed - {(number - 1) % 50 + 1:02}.mkv"
            ),
            root_folder="Anime",
            filename=f"Indexed - {(number - 1) % 50 + 1:02}.mkv",
            size=number,
            mtime_ns=number,
            file_type="episode",
            season_episode_number=(number - 1) % 50 + 1,
        )
        for number in range(1, 501)
    ]
    subtitles = [
        UnresolvedExternalSubtitle(
            id=number,
            relative_path=(
                f"Anime/Indexed/Part {number % 10:02}/Indexed - 01.ass"
            ),
            filename="Indexed - 01.ass",
            extension=".ass",
            language="cs",
            normalized_language="cs",
        )
        for number in range(1, 51)
    ]
    original = subtitle_review_module.detect_episode_number
    parser_calls = 0

    def count_parser(filename):
        nonlocal parser_calls
        parser_calls += 1
        return original(filename)

    monkeypatch.setattr(
        subtitle_review_module, "detect_episode_number", count_parser,
    )
    rows = build_unresolved_subtitle_rows(subtitles, videos)

    assert len(rows) == len(subtitles)
    assert all(row.candidate_count == 1 for row in rows)
    assert parser_calls == len(videos) + len(subtitles)


def test_supplementary_title_get_shows_manual_ordinal_without_writes(performance_app):
    web_app, ids = performance_app
    engine = web_app.state.sessions.kw['bind']
    with Session(engine) as session:
        title = session.get(CatalogTitle, ids['title'])
        title.part_type = 'bonus'
        item = title.videos[0]
        item.filename = 'Show NCOP03.mkv'
        item.file_type = 'ncop'
        item.episode_number_manual_override = 2
        title.videos.append(Video(
            catalog_collection=item.catalog_collection,
            relative_path='Anime/Performance Show/Season 1/Show - 02.mkv',
            root_folder='Anime', filename='Show - 02.mkv', size=2, mtime_ns=2,
            file_type='episode', local_episode_number=2, season_episode_number=2,
            content_type_manual='episode',
        ))
        session.commit()
    endpoint = next(route.endpoint for route in web_app.routes
                    if getattr(route, 'path', None) == '/titles/{catalog_title_id}')
    before = _semantic_snapshot(engine)
    statements = []
    def record(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        response = endpoint(_request(web_app, f"/titles/{ids['title']}"), ids['title'])
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert response.status_code == 200
    rendered = response.body.decode()
    assert '<strong>NCOP 02</strong>' in rendered
    assert 'class="inline-form supplementary-ordinal-form"' in rendered
    assert '<span class="supplementary-ordinal-prefix">NCOP</span>' in rendered
    assert 'Numerický ordinal <input type="number"' in rendered
    assert 'name="manual_episode_number" value="2"' in rendered
    assert 'Výsledná identita: <strong>NCOP 02</strong>' in rendered
    assert 'Zadejte pouze kladné celé číslo.' in rendered
    standard_row = rendered.split('id="video-2"', 1)[1].split('</tr>', 1)[0]
    assert 'supplementary-ordinal-form' not in standard_row
    assert 'placeholder="Číslo dle režimu"' in standard_row
    assert 'aria-label="Ruční číslo epizody podle zvoleného režimu"' in standard_row
    assert not any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for s in statements)
    assert _semantic_snapshot(engine) == before
