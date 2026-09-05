from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.catalog import build_catalog_results
from app.config import Settings
from app.database import Base
from app.main import create_app
from app.metadata.completion import (
    collection_metadata_ok, resolve_metadata_completion, set_metadata_requirement,
)
from app.migrations import migrate_schema, migrate_schema_at_startup
from app.models import CatalogCollection, CatalogTitle, ExternalTitleLink, MetadataCandidate, Video


def make_title(number=1, types=("episode",), part_type="season", confirmed=False):
    collection = CatalogCollection(
        id=number, local_title=f"Show {number}", normalized_local_title=f"show {number}",
        relative_root_path=f"Anime/Show {number}",
    )
    title = CatalogTitle(
        id=number, local_title=f"Title {number}", normalized_local_title=f"title {number}",
        relative_root_path=f"Anime/Show {number}/Season 1", collection=collection,
        part_type=part_type, metadata_status="linked_manual" if confirmed else "unlinked",
    )
    if confirmed:
        title.external_links.append(ExternalTitleLink(
            provider="anilist", external_id=str(number), match_method="manual_search",
            is_primary=True, is_manual=True, verified_at=datetime.now(timezone.utc),
        ))
    for index, file_type in enumerate(types, 1):
        title.videos.append(Video(
            catalog_collection=collection,
            filename=f"Show - {index:02}.mkv", file_type=file_type,
            relative_path=f"{title.relative_root_path}/Show - {index:02}.mkv",
            root_folder="Anime", size=1, mtime_ns=1,
        ))
    return title


@pytest.mark.parametrize("types", [
    ("op",), ("ed",), ("ncop", "nced"), ("menu",), ("cm",),
    ("op", "ed", "ncop", "nced", "menu", "cm"),
])
def test_only_exact_technical_content_is_automatically_not_required(types):
    title = make_title(types=types, part_type="bonus")
    result = resolve_metadata_completion(title, title.videos)
    assert (result.requirement, result.authority, result.state) == (
        "not_required", "automatic", "not_required",
    )
    assert result.resolved


@pytest.mark.parametrize("part_type,types", [
    ("season", ("episode", "ncop")), ("bonus", ("other",)),
    ("bonus", ("pv",)), ("bonus", ("episode",)),
    ("bonus", ("ncop", "other")), ("other", ("other",)),
    ("film", ("other",)), ("ova", ("ova",)), ("special", ("special",)),
    ("recap", ("recap",)), ("preview", ("preview",)),
    ("part", ("episode",)), ("cour", ("episode",)),
])
def test_metadata_relevant_and_mixed_content_remains_required(part_type, types):
    title = make_title(types=types, part_type=part_type)
    result = resolve_metadata_completion(title, title.videos)
    assert result.requirement == "required"
    assert result.state == "missing"
    assert not result.resolved


def test_manual_authority_clear_and_video_override():
    title = make_title(types=("ncop",))
    set_metadata_requirement(title, "required")
    assert not resolve_metadata_completion(title, title.videos).resolved
    set_metadata_requirement(title, "")
    assert title.metadata_requirement_manual is None
    assert resolve_metadata_completion(title, title.videos).resolved
    title.videos[0].content_type_manual = "special"
    assert not resolve_metadata_completion(title, title.videos).resolved
    set_metadata_requirement(title, "not_required")
    result = resolve_metadata_completion(title, title.videos)
    assert result.resolved and result.authority == "manual"
    assert title.part_type == "season"
    assert title.videos[0].file_type == "ncop"
    assert title.videos[0].content_type_manual == "special"
    set_metadata_requirement(title, "")
    assert not resolve_metadata_completion(title, title.videos).resolved


def test_confirmed_relationship_wins_without_deleting_requirement_or_link():
    title = make_title(confirmed=True)
    assert resolve_metadata_completion(title, title.videos).state == "confirmed"
    set_metadata_requirement(title, "not_required")
    assert resolve_metadata_completion(title, title.videos).state == "confirmed"
    assert title.metadata_requirement_manual == "not_required"
    assert title.external_links[0].is_primary


@pytest.mark.parametrize("attribute,value", [
    ("is_manual", False), ("is_primary", False), ("verified_at", None),
])
def test_status_alone_is_not_confirmed_authority(attribute, value):
    title = make_title(confirmed=True)
    setattr(title.external_links[0], attribute, value)
    assert not resolve_metadata_completion(title, title.videos).resolved


@pytest.mark.parametrize("status,rejected", [
    ("candidates_available", False), ("candidates_available", True),
    ("linked_auto", False),
])
def test_candidates_and_rejections_do_not_complete_metadata(status, rejected):
    title = make_title()
    title.metadata_status = status
    title.metadata_candidates.append(MetadataCandidate(
        provider="anilist", external_id="1", candidate_title="Candidate", match_score=.99,
        rejected_at=datetime.now(timezone.utc) if rejected else None,
    ))
    assert not resolve_metadata_completion(title, title.videos).resolved
    assert not build_catalog_results(title.videos, "all").groups[0].metadata_ok


def test_collection_counts_all_populated_titles_including_missing_ova():
    first = make_title(confirmed=True)
    second = make_title(2, types=("ova",), part_type="ova")
    second.collection = first.collection
    second.videos[0].catalog_collection = first.collection
    empty = make_title(3, types=())
    empty.collection = first.collection
    videos = first.videos + second.videos
    assert not build_catalog_results(videos, "all").groups[0].metadata_ok
    set_metadata_requirement(second, "not_required")
    summary = build_catalog_results(videos, "all").groups[0]
    assert summary.metadata_ok and summary.linked_parts == 1
    assert collection_metadata_ok(
        resolve_metadata_completion(title, title.videos) for title in (first, second, empty)
    )
    assert not collection_metadata_ok([resolve_metadata_completion(empty, [])])
    set_metadata_requirement(second, "")
    second.metadata_status = "linked_manual"
    second.external_links.append(ExternalTitleLink(
        provider="anilist", external_id="2", match_method="manual_search",
        is_primary=True, is_manual=True, verified_at=datetime.now(timezone.utc),
    ))
    assert build_catalog_results(videos, "all").groups[0].metadata_ok


@pytest.fixture
def completion_app(tmp_path):
    app = create_app(Settings(
        database_url=f"sqlite:///{tmp_path / 'completion.db'}", anime_path=tmp_path,
        metadata_download_artwork=False, metadata_artwork_directory=tmp_path / "artwork",
    ))
    engine = app.state.sessions.kw["bind"]
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([make_title(1, confirmed=True), make_title(2),
                         make_title(3, types=("ncop",)), make_title(4, types=())])
        session.commit()
    yield app, engine
    engine.dispose()


def endpoint(app, path):
    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)


def request(app, path):
    return Request({"type": "http", "app": app, "method": "GET", "path": path,
                    "root_path": "", "scheme": "http", "query_string": b"",
                    "headers": [], "server": ("testserver", 80)})


def snapshot(engine):
    with engine.connect() as connection:
        return tuple(tuple(connection.execute(table.select().order_by(*table.primary_key)))
                     for table in Base.metadata.sorted_tables)


@pytest.mark.parametrize("path,kwargs", [
    ("/", {}), ("/catalog/{filter_name}", {"filter_name": "all"}),
    ("/metadata-review", {"status": "without"}),
    ("/metadata-review", {"status": "all"}),
    ("/titles/{catalog_title_id}", {"catalog_title_id": 3}),
])
def test_get_is_read_only_and_query_count_bounded_by_titles(completion_app, path, kwargs):
    app, engine = completion_app

    def render():
        sql = []
        def record(_conn, _cursor, statement, *_args):
            sql.append(statement)
        before = snapshot(engine)
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = endpoint(app, path)(request(app, path), **kwargs)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert response.status_code == 200
        assert not any(item.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for item in sql)
        assert snapshot(engine) == before
        return response, len(sql)

    response, baseline = render()
    if path == "/metadata-review" and kwargs["status"] == "without":
        assert [row["title"].id for row in response.context["rows"]] == [2]
    if path == "/titles/{catalog_title_id}":
        assert "Automaticky: metadata nejsou vyžadována" in response.body.decode()
        assert 'name="requirement"' in response.body.decode()
    with Session(engine) as session:
        session.add_all(make_title(number, confirmed=True) for number in range(10, 60))
        session.commit()
    _, expanded = render()
    assert expanded == baseline


@pytest.mark.parametrize("path,kwargs,context_key", [
    ("/", {}, "collections"),
    ("/catalog/{filter_name}", {"filter_name": "all"}, "groups"),
])
def test_metadata_sort_both_directions(completion_app, path, kwargs, context_key):
    app, _engine = completion_app
    for direction, expected in (("asc", [False, True, True]), ("desc", [True, True, False])):
        response = endpoint(app, path)(request(app, path), sort="metadata", direction=direction, **kwargs)
        groups = response.context[context_key]
        if context_key == "collections":
            groups = [row["group"] for row in groups]
        assert [group.metadata_ok for group in groups] == expected
        assert "sort=metadata" in response.body.decode()
        assert "direction=" + ("desc" if direction == "asc" else "asc") in response.body.decode()


def test_homepage_search_keeps_default_relevance_and_explicit_sort_state(completion_app):
    app, _engine = completion_app
    home = endpoint(app, "/")
    default = home(request(app, "/")).body.decode()
    assert 'name="sort"' not in default
    explicit = home(request(app, "/"), sort="metadata", direction="desc").body.decode()
    assert 'name="sort" value="metadata"' in explicit
    assert 'name="direction" value="desc"' in explicit


def test_post_requirement_only_changes_requested_authority(completion_app):
    app, engine = completion_app
    update = endpoint(app, "/titles/{catalog_title_id}/metadata/requirement")
    before = snapshot(engine)
    with pytest.raises(HTTPException) as exc:
        update(2, requirement="bonus", return_url="/metadata-review")
    assert exc.value.status_code == 400
    assert snapshot(engine) == before
    for value in ("not_required", "required", ""):
        response = update(2, requirement=value, return_url="/metadata-review?status=all")
        assert response.status_code == 303
        assert response.headers["location"] == "/metadata-review?status=all"
        with Session(engine) as session:
            title = session.get(CatalogTitle, 2)
            assert title.metadata_requirement_manual == (value or None)
            assert title.metadata_status == "unlinked"
            assert title.part_type == "season"
            assert title.videos[0].content_type_manual is None
    after = snapshot(engine)
    # Only CatalogTitle.updated_at may differ after clearing the override.
    for table, rows_before, rows_after in zip(Base.metadata.sorted_tables, before, after):
        if table.name != "catalog_titles":
            assert rows_before == rows_after


def test_requirement_form_accepts_empty_value_to_clear_override(completion_app):
    import asyncio
    from fastapi.dependencies.utils import request_body_to_args
    from starlette.datastructures import FormData

    app, engine = completion_app
    route = next(route for route in app.routes
                 if getattr(route, "path", None) == "/titles/{catalog_title_id}/metadata/requirement")
    # Exercise FastAPI's actual form decoding: an empty required Form(...) would
    # be rejected before reaching the endpoint, even though direct calls pass.
    for value in ("not_required", ""):
        values, errors = asyncio.run(request_body_to_args(
            route.dependant.body_params,
            FormData({"requirement": value, "return_url": "/metadata-review?status=all"}),
            embed_body_fields=True,
        ))
        assert errors == []
        response = route.endpoint(catalog_title_id=2, **values)
        assert response.status_code == 303
        with Session(engine) as session:
            assert session.get(CatalogTitle, 2).metadata_requirement_manual == (value or None)


def test_schema_upgrade_null_default_and_stable_startup(completion_app):
    _app, engine = completion_app
    migrate_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE catalog_titles DROP COLUMN metadata_requirement_manual"))
        connection.execute(text("PRAGMA user_version = 1"))
    statements = []
    def record(_connection, _cursor, statement, *_args):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert migrate_schema_at_startup(engine)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                   for statement in statements)
    with Session(engine) as session:
        assert all(title.metadata_requirement_manual is None for title in session.scalars(select(CatalogTitle)))
        session.get(CatalogTitle, 2).metadata_requirement_manual = "not_required"
        session.commit()
    before = snapshot(engine)
    migrate_schema(engine)
    assert snapshot(engine) == before
    assert migrate_schema_at_startup(engine) is False
    assert snapshot(engine) == before


def test_rebuild_preserves_requirement_and_rejects_stale_plan(completion_app):
    from app.hierarchy_rebuild import (
        HierarchyPlanStaleError, apply_hierarchy_rebuild_plan, build_hierarchy_rebuild_plan,
    )
    _app, engine = completion_app
    migrate_schema(engine)
    with Session(engine) as session:
        session.get(CatalogTitle, 2).metadata_requirement_manual = "required"
        # An empty title still owns a human decision, so cleanup must preserve it.
        empty = make_title(99, types=())
        empty.metadata_requirement_manual = "not_required"
        session.add(empty)
        session.commit()
        plan = build_hierarchy_rebuild_plan(session)
        item = next(item for item in plan.titles if item.title_id == 99)
        assert "metadata_requirement_manual" in item.protection_reasons
        session.get(CatalogTitle, 2).metadata_requirement_manual = "not_required"
        session.commit()
        with pytest.raises(HierarchyPlanStaleError):
            apply_hierarchy_rebuild_plan(session, plan)
        plan = build_hierarchy_rebuild_plan(session)
        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()
    migrate_schema(engine)
    with Session(engine) as session:
        assert session.get(CatalogTitle, 2).metadata_requirement_manual == "not_required"
        assert session.get(CatalogTitle, 99).metadata_requirement_manual == "not_required"
