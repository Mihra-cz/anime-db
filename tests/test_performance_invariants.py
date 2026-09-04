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
