"""Regression coverage for V5 F1 duplicate presentation consistency.

The canonical tri-state resolver is the only authority on whether a confirmed
duplicate secondary may fold under its primary.  Catalog presentation must give
the same answer as numbering, hierarchy and Media Check:

    VALID   -> collapsed under the primary
    INVALID -> separate active row
    UNKNOWN -> separate active row + review
"""
from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.catalog import is_media_completion_video
from app.catalog_video_presentation import (
    build_catalog_title_video_presentation,
    physical_video_rows,
    ungrouped_presented_video_rows,
)
from app.config import Settings
from app.database import Base, make_engine
from app.main import create_app
from app.models import CatalogCollection, CatalogTitle, Video
from app.numbering import (
    DuplicateRelationState,
    collapses_into_duplicate_primary,
    duplicate_relation_state,
)


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


def _structure(session: Session) -> tuple[CatalogCollection, CatalogTitle]:
    collection = CatalogCollection(
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        collection=collection,
        local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1",
        part_type="season",
        season_number=1,
        season_label="S1",
        sort_order=1,
        numbering_mode="local",
    )
    session.add(collection)
    return collection, title


def _video(
    session: Session,
    collection: CatalogCollection,
    title: CatalogTitle,
    filename: str,
    number: int | None,
    seed: int,
    file_type: str = "episode",
) -> Video:
    video = Video(
        catalog_collection=collection,
        catalog_title=title,
        relative_path=f"Anime/Show/Season 1/{filename}",
        root_folder="Anime",
        filename=filename,
        size=seed,
        mtime_ns=seed,
        file_type=file_type,
        local_episode_number=number,
        season_episode_number=number,
    )
    session.add(video)
    return video


def _pair(session: Session, kind: str) -> tuple[CatalogTitle, Video, Video]:
    """Build one primary/secondary pair whose current relation state is `kind`.

    VALID and INVALID/UNKNOWN deliberately use the same shaped fixture: only the
    current identity of the secondary differs, never the stored evidence.
    """
    collection, title = _structure(session)
    if kind == "valid":
        primary = _video(session, collection, title, "Show - 01.mkv", 1, 1)
        secondary = _video(session, collection, title, "Show - 01 (1080p).mkv", 1, 2)
    elif kind == "invalid":
        # Both stay supplementary, so both land in the same presentation bucket,
        # but their current supplementary identities are provably different.
        primary = _video(session, collection, title, "Show - OVA 01.mkv", None, 1, "ova")
        secondary = _video(session, collection, title, "Show - OVA 02.mkv", None, 2, "ova")
    elif kind == "unknown":
        primary = _video(session, collection, title, "Show - OVA 01.mkv", None, 1, "ova")
        secondary = _video(session, collection, title, "Neznamy soubor.mkv", None, 2, "ova")
    else:
        raise AssertionError(kind)
    session.flush()
    secondary.duplicate_of = primary
    session.flush()
    return title, primary, secondary


def _presentation_state(title, videos, secondary) -> tuple[bool, bool]:
    presentation = build_catalog_title_video_presentation(videos, title)
    own_row = any(
        row.physical.video.id == secondary.id for row in presentation.display_rows
    )
    collapsed = any(
        copy.id == secondary.id
        for row in presentation.display_rows
        for copy in row.physical.duplicate_copies
    )
    return own_row, collapsed


@pytest.fixture
def session():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as opened:
        yield opened
    engine.dispose()


# --------------------------------------------------------------------------
# F1-A / F1-B / F1-C - catalog presentation honours the canonical tri-state
# --------------------------------------------------------------------------


def test_f1_c_valid_secondary_stays_collapsed_under_primary(session):
    title, _primary, secondary = _pair(session, "valid")
    assert duplicate_relation_state(secondary) == DuplicateRelationState.VALID

    own_row, collapsed = _presentation_state(title, list(title.videos), secondary)

    assert collapsed is True
    assert own_row is False


def test_f1_b_invalid_secondary_keeps_its_own_physical_row(session):
    title, _primary, secondary = _pair(session, "invalid")
    assert duplicate_relation_state(secondary) == DuplicateRelationState.INVALID

    own_row, collapsed = _presentation_state(title, list(title.videos), secondary)

    assert collapsed is False
    assert own_row is True


def test_f1_a_unknown_secondary_keeps_its_own_physical_row(session):
    title, _primary, secondary = _pair(session, "unknown")
    assert duplicate_relation_state(secondary) == DuplicateRelationState.UNKNOWN

    own_row, collapsed = _presentation_state(title, list(title.videos), secondary)

    assert collapsed is False
    assert own_row is True


@pytest.mark.parametrize("kind", ["invalid", "unknown"])
def test_f1_physical_video_rows_helper_does_not_collapse_non_valid(session, kind):
    title, primary, secondary = _pair(session, kind)
    videos = list(title.videos)

    rows = physical_video_rows(videos, {video.id: video for video in videos})

    assert {row.video.id for row in rows} == {primary.id, secondary.id}
    assert all(row.duplicate_copies == () for row in rows)


def test_f1_physical_video_rows_helper_still_collapses_valid(session):
    title, primary, secondary = _pair(session, "valid")
    videos = list(title.videos)

    rows = physical_video_rows(videos, {video.id: video for video in videos})

    assert {row.video.id for row in rows} == {primary.id}
    assert rows[0].duplicate_copies[0].id == secondary.id


@pytest.mark.parametrize("kind", ["invalid", "unknown"])
def test_f1_compatibility_presentation_does_not_collapse_non_valid(session, kind):
    """`ungrouped_presented_video_rows` is a detached path with the same rule."""
    title, primary, secondary = _pair(session, kind)
    videos = list(title.videos)

    rows = ungrouped_presented_video_rows(
        videos, {video.id: video for video in videos},
    )

    assert {row.physical.video.id for row in rows} == {primary.id, secondary.id}


def test_f1_compatibility_presentation_still_collapses_valid(session):
    title, primary, secondary = _pair(session, "valid")
    videos = list(title.videos)

    rows = ungrouped_presented_video_rows(
        videos, {video.id: video for video in videos},
    )

    assert {row.physical.video.id for row in rows} == {primary.id}
    assert rows[0].physical.duplicate_copies[0].id == secondary.id


# --------------------------------------------------------------------------
# Missing primary / orphan display semantics are preserved
# --------------------------------------------------------------------------


def test_f1_missing_primary_keeps_a_visible_orphan_row(session):
    collection, title = _structure(session)
    secondary = _video(session, collection, title, "Show - 01.mkv", 1, 1)
    session.flush()
    secondary.duplicate_primary_missing = True
    session.flush()
    assert duplicate_relation_state(secondary) == DuplicateRelationState.INVALID

    rows = physical_video_rows([secondary], {secondary.id: secondary})

    assert [row.video.id for row in rows] == [secondary.id]
    assert rows[0].orphan_duplicate is True


def test_f1_contradictory_missing_primary_flag_never_collapses(session):
    """duplicate_primary_missing is INVALID even while the old FK still resolves."""
    title, primary, secondary = _pair(session, "valid")
    secondary.duplicate_primary_missing = True
    session.flush()
    assert duplicate_relation_state(secondary) == DuplicateRelationState.INVALID

    videos = list(title.videos)
    rows = physical_video_rows(videos, {video.id: video for video in videos})

    assert {row.video.id for row in rows} == {primary.id, secondary.id}


# --------------------------------------------------------------------------
# Cross-layer consistency
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected_state", "expected_collapse"),
    (
        ("valid", DuplicateRelationState.VALID, True),
        ("invalid", DuplicateRelationState.INVALID, False),
        ("unknown", DuplicateRelationState.UNKNOWN, False),
    ),
)
def test_f1_every_layer_gives_the_same_answer(
    session, kind, expected_state, expected_collapse,
):
    title, _primary, secondary = _pair(session, kind)
    videos = list(title.videos)

    assert duplicate_relation_state(secondary) == expected_state
    assert collapses_into_duplicate_primary(secondary) is expected_collapse
    # Media Check counts a video as a completion unit exactly when it does not
    # currently fold away.
    assert is_media_completion_video(secondary) is not expected_collapse

    own_row, collapsed = _presentation_state(title, videos, secondary)
    assert collapsed is expected_collapse
    assert own_row is not expected_collapse

    compatibility_ids = {
        row.physical.video.id
        for row in ungrouped_presented_video_rows(
            videos, {video.id: video for video in videos},
        )
    }
    assert (secondary.id in compatibility_ids) is not expected_collapse


# --------------------------------------------------------------------------
# Rendered GET /titles/{id}
# --------------------------------------------------------------------------


@pytest.fixture
def rendered_app(tmp_path):
    def build(kind: str):
        web_app = create_app(Settings(
            anime_path=tmp_path,
            database_url=f"sqlite:///{tmp_path / f'f1-{kind}.db'}",
            metadata_download_artwork=False,
            metadata_artwork_directory=tmp_path / "artwork",
        ))
        engine = web_app.state.sessions.kw["bind"]
        Base.metadata.create_all(engine)
        with Session(engine) as opened:
            title, primary, secondary = _pair(opened, kind)
            opened.commit()
            ids = (title.id, primary.id, secondary.id)
        return web_app, engine, ids

    return build


def _render_title_detail(web_app, engine, title_id):
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/titles/{catalog_title_id}"
    )
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = endpoint(_request(web_app, f"/titles/{title_id}"), title_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200
    return response.body.decode(), statements


@pytest.mark.parametrize("kind", ["invalid", "unknown"])
def test_f1_rendered_title_detail_shows_non_valid_secondary_as_its_own_row(
    rendered_app, kind,
):
    web_app, engine, (title_id, primary_id, secondary_id) = rendered_app(kind)

    rendered, statements = _render_title_detail(web_app, engine, title_id)

    assert f'id="video-{primary_id}"' in rendered
    assert f'id="video-{secondary_id}"' in rendered
    assert "potvrzená duplicitní kopie" not in rendered
    assert not any(
        statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in statements
    )


def test_f1_rendered_title_detail_still_collapses_a_valid_secondary(rendered_app):
    web_app, engine, (title_id, primary_id, secondary_id) = rendered_app("valid")

    rendered, statements = _render_title_detail(web_app, engine, title_id)

    assert f'id="video-{primary_id}"' in rendered
    assert f'id="video-{secondary_id}"' not in rendered
    assert "potvrzená duplicitní kopie" in rendered
    assert not any(
        statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in statements
    )


def test_f1_rendered_title_detail_stays_semantically_read_only(rendered_app):
    web_app, engine, (title_id, _primary_id, _secondary_id) = rendered_app("unknown")

    def snapshot():
        with engine.connect() as connection:
            return tuple(
                (
                    table.name,
                    tuple(tuple(row) for row in connection.execute(
                        table.select().order_by(*table.primary_key.columns)
                    )),
                )
                for table in sorted(
                    Base.metadata.tables.values(), key=lambda item: item.name
                )
            )

    before = snapshot()
    _render_title_detail(web_app, engine, title_id)
    assert snapshot() == before
