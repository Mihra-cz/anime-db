"""Explicit confirmation of unnumbered supplementary copies."""

import asyncio
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import event
from starlette.requests import Request

from app.config import Settings
from app.database import Base
from app.main import create_app
from app.models import CatalogCollection, CatalogTitle, Video, VideoVariantGroup
from app.numbering import DuplicateRelationState, duplicate_relation_state, set_duplicate_group_primary
from app.supplementary import supplementary_inventory, supplementary_review_issues
from app.hierarchy_review import (
    clear_confirmed_duplicate_videos,
    confirm_unnumbered_supplementary_copies,
    preview_unnumbered_supplementary_copies,
)


def post_form(app, route_path, actual_path, values, collection_id):
    body = urlencode(values).encode()
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request = Request({
        "type": "http", "method": "POST", "path": actual_path,
        "root_path": "", "scheme": "http", "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "server": ("testserver", 80), "client": ("testclient", 50000),
        "app": app,
    }, receive)
    endpoint = next(route.endpoint for route in app.routes if route.path == route_path)
    return asyncio.run(endpoint(request, collection_id))


def film_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'films.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(local_title="Tenki no Ko", normalized_local_title="tenki no ko", relative_root_path="Tenki no Ko")
        title = CatalogTitle(collection=collection, local_title="Tenki no Ko", normalized_local_title="tenki no ko", relative_root_path="Tenki no Ko", part_type="film")
        filenames = ("Tenki no Ko.mkv", "TenkiNoKo.m4v", "Weathering.with.You.mkv")
        videos = [Video(catalog_title=title, catalog_collection=collection, relative_path=f"Tenki no Ko/{name}", root_folder="Tenki no Ko", filename=name, size=1, mtime_ns=1, file_type="film") for name in filenames]
        session.add_all(videos)
        session.commit()
        return engine, collection.id, title.id, [v.id for v in videos]


def load(session, collection_id):
    return session.scalar(select(CatalogCollection).options(
        selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
        selectinload(CatalogCollection.videos).selectinload(Video.duplicate_of),
        selectinload(CatalogCollection.videos).selectinload(Video.duplicate_copies),
        selectinload(CatalogCollection.videos).selectinload(Video.video_variant_group),
    ).where(CatalogCollection.id == collection_id))


def test_film_suspicion_preview_confirmation_reload_and_clear(tmp_path):
    engine, collection_id, title_id, ids = film_db(tmp_path)
    with Session(engine) as session:
        collection = load(session, collection_id)
        title = collection.titles[0]
        videos = sorted(collection.videos, key=lambda v: v.id)
        assert [i.code for i in supplementary_review_issues(videos, title)] == ["missing_supplementary_ordinal"]
        videos[1].duplicate_status_manual = videos[2].duplicate_status_manual = "suspected"
        session.commit()
        assert [i.code for i in supplementary_review_issues(videos, title)] == ["missing_supplementary_ordinal"]
        with pytest.raises(ValueError, match="známou logickou identitu"):
            set_duplicate_group_primary(videos, videos[0])
        preview = preview_unnumbered_supplementary_copies(session, collection_id, ids, ids[0])
        assert preview.primary.filename == "Tenki no Ko.mkv"
        assert {v.filename for v in preview.secondaries} == {"TenkiNoKo.m4v", "Weathering.with.You.mkv"}
        assert preview.supplementary_type == "film"
        assert not session.dirty
        assert [v.duplicate_of_video_id for v in videos] == [None, None, None]
        confirm_unnumbered_supplementary_copies(session, collection_id, ids, ids[0], preview.fingerprint)
        session.commit()
    with Session(engine) as session:
        collection = load(session, collection_id)
        videos = sorted(collection.videos, key=lambda v: v.id)
        assert [v.duplicate_of_video_id for v in videos] == [None, ids[0], ids[0]]
        assert [v.duplicate_confirmation_kind for v in videos] == [None, "unnumbered_supplementary_same_content", "unnumbered_supplementary_same_content"]
        assert all(duplicate_relation_state(v) == DuplicateRelationState.VALID for v in videos[1:])
        assert supplementary_inventory(videos, collection.titles[0]).logical_identity_count == 1
        assert supplementary_review_issues(videos, collection.titles[0]) == ()
        clear_confirmed_duplicate_videos(session, collection_id, ids)
        session.commit()
    with Session(engine) as session:
        videos = list(session.scalars(select(Video).order_by(Video.id)))
        assert all(v.duplicate_of_video_id is None and v.duplicate_confirmation_kind is None for v in videos)


def test_legacy_unknown_and_current_identity_changes(tmp_path):
    engine, collection_id, _, ids = film_db(tmp_path)
    with Session(engine) as session:
        videos = [session.get(Video, i) for i in ids]
        videos[1].duplicate_of = videos[0]
        session.commit()
    with Session(engine) as session:
        secondary = session.get(Video, ids[1])
        assert duplicate_relation_state(secondary) == DuplicateRelationState.UNKNOWN
        secondary.duplicate_of = None
        session.flush()
        preview = preview_unnumbered_supplementary_copies(session, collection_id, ids, ids[0])
        confirm_unnumbered_supplementary_copies(session, collection_id, ids, ids[0], preview.fingerprint)
        session.commit()
    with Session(engine) as session:
        secondary = session.get(Video, ids[1])
        secondary.content_type_manual = "bonus"
        assert duplicate_relation_state(secondary) == DuplicateRelationState.INVALID
        secondary.content_type_manual = None
        secondary.episode_number_manual_override = 1
        assert duplicate_relation_state(secondary) == DuplicateRelationState.INVALID


@pytest.mark.parametrize("change", [
    "different_title", "mixed_type", "variant_conflict", "media_part",
    "episode", "orphan_marker", "external_copy",
])
def test_unsafe_selection_is_rejected(tmp_path, change):
    engine, collection_id, _, ids = film_db(tmp_path)
    with Session(engine) as session:
        videos = [session.get(Video, i) for i in ids]
        if change == "different_title":
            new_title = CatalogTitle(collection=videos[0].catalog_title.collection, local_title="Other", normalized_local_title="other", relative_root_path="Tenki no Ko/Other", part_type="film")
            videos[2].catalog_title = new_title
        elif change == "mixed_type":
            videos[2].content_type_manual = "bonus"
        elif change == "variant_conflict":
            title = videos[0].catalog_title
            group_a = VideoVariantGroup(catalog_title=title, manual_label="BD")
            group_b = VideoVariantGroup(catalog_title=title, manual_label="TV")
            videos[0].video_variant_group = group_a
            videos[1].video_variant_group = group_b
        elif change == "media_part":
            videos[1].media_part_number = 1
        elif change == "orphan_marker":
            videos[1].duplicate_confirmation_kind = "unnumbered_supplementary_same_content"
        elif change == "external_copy":
            outsider = Video(
                catalog_title=videos[0].catalog_title,
                catalog_collection=videos[0].catalog_collection,
                relative_path="Tenki no Ko/Other copy.mkv", root_folder="Tenki no Ko",
                filename="Other copy.mkv", size=1, mtime_ns=1, file_type="film",
                duplicate_of=videos[0],
                duplicate_confirmation_kind="unnumbered_supplementary_same_content",
            )
            session.add(outsider)
        else:
            videos[1].content_type_manual = "episode"
        session.flush()
        with pytest.raises(ValueError):
            preview_unnumbered_supplementary_copies(session, collection_id, ids, ids[0])


def test_stale_preview_rejects_without_partial_write(tmp_path):
    engine, collection_id, _, ids = film_db(tmp_path)
    with Session(engine) as session:
        preview = preview_unnumbered_supplementary_copies(session, collection_id, ids, ids[0])
        session.get(Video, ids[2]).media_part_number = 1
        session.flush()
        with pytest.raises(ValueError):
            confirm_unnumbered_supplementary_copies(session, collection_id, ids, ids[0], preview.fingerprint)
        assert all(session.get(Video, i).duplicate_of_video_id is None for i in ids)


def test_change_primary_transfers_marker_and_clear_removes_it(tmp_path):
    engine, collection_id, _, ids = film_db(tmp_path)
    with Session(engine) as session:
        first = preview_unnumbered_supplementary_copies(session, collection_id, ids, ids[0])
        confirm_unnumbered_supplementary_copies(session, collection_id, ids, ids[0], first.fingerprint)
        session.commit()
    with Session(engine) as session:
        changed = preview_unnumbered_supplementary_copies(session, collection_id, ids, ids[1])
        confirm_unnumbered_supplementary_copies(session, collection_id, ids, ids[1], changed.fingerprint)
        session.commit()
    with Session(engine) as session:
        videos = [session.get(Video, i) for i in ids]
        assert [v.duplicate_of_video_id for v in videos] == [ids[1], None, ids[1]]
        assert [v.duplicate_confirmation_kind for v in videos] == [
            "unnumbered_supplementary_same_content", None,
            "unnumbered_supplementary_same_content",
        ]
        assert all(duplicate_relation_state(v) == DuplicateRelationState.VALID for v in (videos[0], videos[2]))
        clear_confirmed_duplicate_videos(session, collection_id, ids)
        session.commit()
    with Session(engine) as session:
        assert all(
            session.get(Video, i).duplicate_of_video_id is None
            and session.get(Video, i).duplicate_confirmation_kind is None
            for i in ids
        )


def test_confirm_rolls_back_all_members_on_late_failure(tmp_path, monkeypatch):
    engine, collection_id, _, ids = film_db(tmp_path)
    with Session(engine) as session:
        preview = preview_unnumbered_supplementary_copies(session, collection_id, ids, ids[0])
        import app.hierarchy_review as review
        def fail_after_relation(_collection, *, recalculate=True):
            raise ValueError("late failure")
        monkeypatch.setattr(review, "refresh_collection_state", fail_after_relation)
        with pytest.raises(ValueError, match="late failure"):
            confirm_unnumbered_supplementary_copies(session, collection_id, ids, ids[0], preview.fingerprint)
        session.rollback()
    with Session(engine) as session:
        assert all(session.get(Video, i).duplicate_of_video_id is None for i in ids)
        assert all(session.get(Video, i).duplicate_confirmation_kind is None for i in ids)


def test_confirmed_relation_stops_collapsing_after_title_or_context_change(tmp_path):
    engine, collection_id, _, ids = film_db(tmp_path)
    with Session(engine) as session:
        preview = preview_unnumbered_supplementary_copies(session, collection_id, ids, ids[0])
        confirm_unnumbered_supplementary_copies(session, collection_id, ids, ids[0], preview.fingerprint)
        session.commit()
    with Session(engine) as session:
        secondary = session.get(Video, ids[1])
        new_title = CatalogTitle(collection=secondary.catalog_collection, local_title="Other", normalized_local_title="other", relative_root_path="Tenki no Ko/Other", part_type="film")
        secondary.catalog_title = new_title
        session.flush()
        assert duplicate_relation_state(secondary) == DuplicateRelationState.INVALID


@pytest.mark.parametrize("change", ["variant_conflict", "media_part", "different_collection"])
def test_confirmed_relation_invalidates_when_current_evidence_changes(tmp_path, change):
    engine, collection_id, _, ids = film_db(tmp_path)
    with Session(engine) as session:
        preview = preview_unnumbered_supplementary_copies(session, collection_id, ids, ids[0])
        confirm_unnumbered_supplementary_copies(session, collection_id, ids, ids[0], preview.fingerprint)
        session.commit()
    with Session(engine) as session:
        primary, secondary = [session.get(Video, i) for i in ids[:2]]
        if change == "variant_conflict":
            title = primary.catalog_title
            primary.video_variant_group = VideoVariantGroup(catalog_title=title, manual_label="BD")
            secondary.video_variant_group = VideoVariantGroup(catalog_title=title, manual_label="TV")
        elif change == "media_part":
            secondary.media_part_number = 1
        else:
            secondary.catalog_collection = CatalogCollection(
                local_title="Other", normalized_local_title="other",
                relative_root_path="Other",
            )
        session.flush()
        assert duplicate_relation_state(secondary) == DuplicateRelationState.INVALID
        assert secondary.duplicate_of_video_id == ids[0]
        inventory = supplementary_inventory(list(session.scalars(
            select(Video).where(Video.catalog_title_id == primary.catalog_title_id)
        )), primary.catalog_title)
        assert secondary in inventory.invalid_duplicates
        assert inventory.logical_identity_count is None


def test_hierarchy_get_and_preview_are_read_only_on_temp_db(tmp_path):
    engine, collection_id, title_id, ids = film_db(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA user_version = 5"))
    engine.dispose()
    app = create_app(Settings(
        anime_path=tmp_path / "media", database_url=f"sqlite:///{tmp_path / 'films.db'}",
        metadata_download_artwork=False, metadata_artwork_directory=tmp_path / "artwork",
    ))
    statements = []
    def record(_connection, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER")):
            statements.append(statement)
    app_engine = app.state.sessions.kw["bind"]
    event.listen(app_engine, "before_cursor_execute", record)
    try:
        get_path = f"/hierarchy-review/{collection_id}/titles/{title_id}"
        get_endpoint = next(route.endpoint for route in app.routes if route.path == "/hierarchy-review/{collection_id}/titles/{catalog_title_id}")
        get_request = Request({
            "type": "http", "method": "GET", "path": get_path,
            "root_path": "", "scheme": "http", "query_string": b"",
            "headers": [], "server": ("testserver", 80), "client": ("testclient", 50000),
            "app": app,
        })
        response = get_endpoint(get_request, collection_id, title_id)
        assert response.status_code == 200
        assert "Potvrdit vybrané soubory jako fyzické kopie stejného obsahu" in response.body.decode()
        route_path = "/hierarchy-review/{collection_id}/duplicates/unnumbered/preview"
        response = post_form(
            app, route_path, f"/hierarchy-review/{collection_id}/duplicates/unnumbered/preview",
            [("video_ids", str(i)) for i in ids] + [("primary_video_id", str(ids[0]))], collection_id,
        )
        assert response.status_code == 200
        assert "3 fyzická videa → 1 logická Film identita" in response.body.decode()
        assert "Supplementary ordinal zůstane neurčen" in response.body.decode()
        assert statements == []
    finally:
        event.remove(app_engine, "before_cursor_execute", record)


def test_http_confirmation_requires_explicit_checkbox_and_persists(tmp_path):
    engine, collection_id, _, ids = film_db(tmp_path)
    app = create_app(Settings(
        anime_path=tmp_path / "media", database_url=f"sqlite:///{tmp_path / 'films.db'}",
        metadata_download_artwork=False, metadata_artwork_directory=tmp_path / "artwork",
    ))
    with app.state.sessions() as session:
        preview = preview_unnumbered_supplementary_copies(session, collection_id, ids, ids[0])
    route_path = "/hierarchy-review/{collection_id}/duplicates/unnumbered/confirm"
    actual_path = f"/hierarchy-review/{collection_id}/duplicates/unnumbered/confirm"
    values = [("video_ids", str(i)) for i in ids] + [
        ("primary_video_id", str(ids[0])), ("expected_fingerprint", preview.fingerprint),
    ]
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        post_form(app, route_path, actual_path, values, collection_id)
    assert error.value.status_code == 400
    with app.state.sessions() as session:
        assert all(session.get(Video, i).duplicate_of_video_id is None for i in ids)
    response = post_form(
        app, route_path, actual_path,
        values + [("confirm_same_content", "true")], collection_id,
    )
    assert response.status_code in {302, 303, 307}
    with app.state.sessions() as session:
        assert [session.get(Video, i).duplicate_of_video_id for i in ids] == [None, ids[0], ids[0]]
