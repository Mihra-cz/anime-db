from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import Settings
from app.database import Base
from app.main import create_app
from app.metadata.completion import has_confirmed_metadata, resolve_metadata_completion
from app.metadata.link_lifecycle import (
    ExternalTitleLinkLifecycle, active_primary_external_link,
    confirmed_primary_external_link, external_title_link_is_active,
)
from app.metadata.providers.base import ProviderTitleMetadata
from app.metadata.service import (
    MetadataConflictError, confirm_anilist_candidate, refresh_title_metadata,
    unlink_title_metadata,
)
from app.metadata.split import evaluate_metadata_split
from app.migrations import (
    STARTUP_COMPATIBILITY_VERSION, migrate_schema, migrate_schema_at_startup,
)
from app.models import (
    CatalogCollection, CatalogTitle, ExternalTitleLink, TitleMetadata, Video,
)
from app.numbering import _confirmed_expected_episode_count

ACTIVE = ExternalTitleLinkLifecycle.ACTIVE.value
SUPERSEDED = ExternalTitleLinkLifecycle.SUPERSEDED.value
UNLINKED = ExternalTitleLinkLifecycle.UNLINKED.value
LEGACY = ExternalTitleLinkLifecycle.LEGACY_HISTORICAL.value
VERIFIED = datetime(2026, 9, 3, 20, 39, tzinfo=timezone.utc)


class Provider:
    def __init__(self, *ids):
        self.fetched = []
        self.titles = {
            external_id: ProviderTitleMetadata(
                provider="anilist", external_id=external_id,
                title_english=f"Title {external_id}", episode_count=12,
                site_url=f"https://anilist.co/anime/{external_id}",
            )
            for external_id in ids
        }

    def fetch_title(self, external_id):
        self.fetched.append(str(external_id))
        return self.titles[str(external_id)]


@pytest.fixture
def engine(tmp_path):
    value = create_engine(f"sqlite:///{tmp_path / 'links.db'}")
    Base.metadata.create_all(value)
    yield value
    value.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as value:
        yield value


def persisted_links(engine, title_id):
    """Reload link rows through a new session so the identity map cannot hide state."""
    with Session(engine) as fresh:
        return {
            link.external_id: (
                link.lifecycle_state, link.is_primary, link.is_manual,
                link.verified_at is not None,
            )
            for link in fresh.scalars(select(ExternalTitleLink).where(
                ExternalTitleLink.catalog_title_id == title_id,
            ))
        }


def add_title(session, name="Local"):
    collection = CatalogCollection(
        local_title=name, normalized_local_title=name.casefold(),
        relative_root_path=f"Anime/{name}",
    )
    title = CatalogTitle(
        collection=collection, local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=f"Anime/{name}/Season 1",
        part_type="season", season_number=1,
    )
    title.videos.append(Video(
        catalog_collection=collection, relative_path=f"Anime/{name}/Season 1/E01.mkv",
        root_folder="Anime", filename="E01.mkv", size=1, mtime_ns=1,
        file_type="episode",
    ))
    session.add(title)
    session.flush()
    return title


def link_row(external_id, lifecycle, *, primary=None, manual=True):
    return ExternalTitleLink(
        provider="anilist", external_id=external_id, match_method="manual_search",
        is_primary=(lifecycle in {None, ACTIVE}) if primary is None else primary,
        is_manual=manual, verified_at=VERIFIED if manual else None,
        lifecycle_state=lifecycle,
    )


def confirmed_looking_title(session, lifecycle, *, primary=None):
    """Title whose status/payload look confirmed; only the link lifecycle varies."""
    title = add_title(session, f"Case {lifecycle}")
    title.metadata_status = "linked_manual"
    title.metadata_record = TitleMetadata(
        display_title="Payload", episode_count=1,
        metadata_provider="anilist", metadata_external_id="1",
    )
    title.external_links.append(link_row("1", lifecycle, primary=primary))
    session.flush()
    return title


# --- confirm / change / reselect ---------------------------------------------

def test_fresh_confirm_creates_active_manual_primary(engine, session):
    title = add_title(session)
    confirm_anilist_candidate(session, title, "1", Provider("1"))
    session.commit()
    assert persisted_links(engine, title.id) == {"1": (ACTIVE, True, True, True)}
    with Session(engine) as fresh:
        reloaded = fresh.get(CatalogTitle, title.id)
        assert has_confirmed_metadata(reloaded)
        assert confirmed_primary_external_link(reloaded).external_id == "1"


def test_changing_provider_id_supersedes_previous_active_link(engine, session):
    title = add_title(session)
    provider = Provider("1", "2")
    first = confirm_anilist_candidate(session, title, "1", provider)
    session.commit()
    first_id = first.id
    with Session(engine) as fresh:
        first_verified = fresh.get(ExternalTitleLink, first_id).verified_at
    confirm_anilist_candidate(session, title, "2", provider)
    session.commit()
    assert persisted_links(engine, title.id) == {
        "1": (SUPERSEDED, False, True, True),
        "2": (ACTIVE, True, True, True),
    }
    with Session(engine) as fresh:
        # The historical fact of the earlier manual confirmation is preserved.
        assert fresh.get(ExternalTitleLink, first_id).verified_at == first_verified
        reloaded = fresh.get(CatalogTitle, title.id)
        assert confirmed_primary_external_link(reloaded).external_id == "2"
        assert fresh.get(TitleMetadata, title.id).metadata_external_id == "2"


def test_reselecting_superseded_link_reactivates_the_same_row(engine, session):
    title = add_title(session)
    provider = Provider("1", "2")
    first_id = confirm_anilist_candidate(session, title, "1", provider).id
    confirm_anilist_candidate(session, title, "2", provider)
    # The reactivated row has the lower id; the partial unique primary index
    # must still hold when both rows change within one explicit workflow.
    reactivated_id = confirm_anilist_candidate(session, title, "1", provider).id
    session.commit()
    assert reactivated_id == first_id
    assert persisted_links(engine, title.id) == {
        "1": (ACTIVE, True, True, True),
        "2": (SUPERSEDED, False, True, True),
    }
    with Session(engine) as fresh:
        assert fresh.scalar(select(func.count()).select_from(ExternalTitleLink)) == 2


def test_reconfirming_active_link_keeps_it_active(engine, session):
    title = add_title(session)
    provider = Provider("1")
    link_id = confirm_anilist_candidate(session, title, "1", provider).id
    again_id = confirm_anilist_candidate(session, title, "1", provider).id
    session.commit()
    assert again_id == link_id
    assert persisted_links(engine, title.id) == {"1": (ACTIVE, True, True, True)}


def test_reselecting_legacy_or_unlinked_row_reactivates_without_duplicate(engine, session):
    title = add_title(session)
    title.external_links.extend([link_row("7", LEGACY), link_row("8", UNLINKED)])
    session.commit()
    provider = Provider("7", "8")
    confirm_anilist_candidate(session, title, "7", provider)
    confirm_anilist_candidate(session, title, "8", provider)
    session.commit()
    assert persisted_links(engine, title.id) == {
        "7": (SUPERSEDED, False, True, True),
        "8": (ACTIVE, True, True, True),
    }


# --- unlink ------------------------------------------------------------------

def test_unlink_moves_only_active_link_to_unlinked(engine, session):
    title = add_title(session)
    provider = Provider("1", "2")
    confirm_anilist_candidate(session, title, "1", provider)
    confirm_anilist_candidate(session, title, "2", provider)
    unlink_title_metadata(session, title)
    session.commit()
    assert persisted_links(engine, title.id) == {
        "1": (SUPERSEDED, False, True, True),
        "2": (UNLINKED, False, True, True),
    }
    with Session(engine) as fresh:
        reloaded = fresh.get(CatalogTitle, title.id)
        assert fresh.get(TitleMetadata, title.id) is None
        assert (reloaded.metadata_status, reloaded.preferred_external_id) == ("unlinked", None)
        assert active_primary_external_link(reloaded.external_links) is None
        assert not has_confirmed_metadata(reloaded)


def test_unlink_preserves_legacy_and_previously_unlinked_rows(engine, session):
    title = add_title(session)
    title.external_links.extend([link_row("7", LEGACY), link_row("8", UNLINKED)])
    session.commit()
    confirm_anilist_candidate(session, title, "1", Provider("1"))
    unlink_title_metadata(session, title)
    unlink_title_metadata(session, title)
    session.commit()
    assert persisted_links(engine, title.id) == {
        "1": (UNLINKED, False, True, True),
        "7": (LEGACY, False, True, True),
        "8": (UNLINKED, False, True, True),
    }


# --- read authority ------------------------------------------------------------

@pytest.mark.parametrize("lifecycle", [SUPERSEDED, UNLINKED, LEGACY])
def test_historical_link_is_never_metadata_authority(session, lifecycle):
    title = confirmed_looking_title(session, lifecycle)
    link = title.external_links[0]
    assert link.is_manual and link.verified_at is not None and not link.is_primary
    assert not external_title_link_is_active(link)
    assert active_primary_external_link(title.external_links) is None
    assert not has_confirmed_metadata(title)
    assert resolve_metadata_completion(title, title.videos).state == "missing"
    assert _confirmed_expected_episode_count(title) is None
    assert evaluate_metadata_split(title) is None


@pytest.mark.parametrize("lifecycle", [ACTIVE, None])
def test_active_primary_is_confirmed_authority_including_legacy_null(session, lifecycle):
    title = confirmed_looking_title(session, lifecycle)
    assert external_title_link_is_active(title.external_links[0])
    assert has_confirmed_metadata(title)
    assert resolve_metadata_completion(title, title.videos).state == "confirmed"
    assert _confirmed_expected_episode_count(title) == 1


@pytest.mark.parametrize("lifecycle,primary", [
    (ACTIVE, False), (SUPERSEDED, True), (UNLINKED, True), (LEGACY, True), (None, False),
])
def test_primary_flag_and_active_lifecycle_are_both_required(session, lifecycle, primary):
    title = confirmed_looking_title(session, lifecycle, primary=primary)
    assert not external_title_link_is_active(title.external_links[0])
    assert not has_confirmed_metadata(title)


def test_completion_still_requires_manual_verified_active_primary(session):
    title = add_title(session)
    title.metadata_status = "linked_manual"
    title.external_links.append(link_row("1", ACTIVE, manual=False))
    session.flush()
    assert active_primary_external_link(title.external_links) is not None
    assert confirmed_primary_external_link(title) is None
    assert resolve_metadata_completion(title, title.videos).state == "missing"


def test_refresh_uses_only_active_primary(session):
    title = add_title(session)
    provider = Provider("1", "2")
    confirm_anilist_candidate(session, title, "1", provider)
    confirm_anilist_candidate(session, title, "2", provider)
    provider.fetched.clear()
    refresh_title_metadata(session, title, provider)
    assert provider.fetched == ["2"]

    historical_only = add_title(session, "Historical")
    historical_only.external_links.extend([link_row("1", SUPERSEDED), link_row("3", LEGACY)])
    session.flush()
    with pytest.raises(ValueError, match="nemá primární AniList vazbu"):
        refresh_title_metadata(session, historical_only, provider)


@pytest.mark.parametrize("lifecycle", [SUPERSEDED, UNLINKED, LEGACY])
def test_conflict_guard_ignores_historical_links_of_other_titles(engine, session, lifecycle):
    other = add_title(session, "Other")
    other.external_links.append(link_row("1", lifecycle))
    title = add_title(session, "Target")
    session.commit()
    confirm_anilist_candidate(session, title, "1", Provider("1"))
    session.commit()
    assert persisted_links(engine, title.id) == {"1": (ACTIVE, True, True, True)}
    assert persisted_links(engine, other.id)["1"][:2] == (lifecycle, False)


@pytest.mark.parametrize("lifecycle", [ACTIVE, None])
def test_conflict_guard_still_blocks_active_primary_of_other_title(engine, session, lifecycle):
    other = add_title(session, "Other")
    other.external_links.append(link_row("1", lifecycle))
    title = add_title(session, "Target")
    session.commit()
    with pytest.raises(MetadataConflictError):
        confirm_anilist_candidate(session, title, "1", Provider("1"))
    session.rollback()
    assert persisted_links(engine, title.id) == {}
    confirm_anilist_candidate(session, title, "1", Provider("1"), confirm_conflict=True)
    session.commit()
    assert persisted_links(engine, title.id) == {"1": (ACTIVE, True, True, True)}
    assert persisted_links(engine, other.id)["1"][:2] == (lifecycle, True)


# --- migration v5 -> v6 ------------------------------------------------------

def _v5_database(tmp_path):
    """Build a v5-shaped database: same schema, but no lifecycle column yet."""
    engine = create_engine(f"sqlite:///{tmp_path / 'v5-links.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p1 = add_title(db, "P1")
        p2 = add_title(db, "P2")
        unlinked = add_title(db, "Unlinked")
        for title, external_id in ((p1, "117612"), (p2, "139648")):
            title.metadata_status = "linked_manual"
            title.metadata_record = TitleMetadata(
                display_title=external_id, episode_count=13,
                metadata_provider="anilist", metadata_external_id=external_id,
            )
            title.external_links.append(link_row(external_id, None, primary=True))
        # Historical non-primary rows: replaced, auto, and pre-lifecycle unlink.
        p2.external_links.append(link_row("117612", None, primary=False))
        p2.external_links.append(link_row("555", None, primary=False, manual=False))
        unlinked.external_links.append(link_row("999", None, primary=False))
        db.commit()
        completion_before = {
            title.local_title: resolve_metadata_completion(title, title.videos).state
            for title in db.scalars(select(CatalogTitle))
        }
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE external_title_links DROP COLUMN lifecycle_state"))
        connection.execute(text("PRAGMA user_version = 5"))
    return engine, completion_before


def _link_rows(engine):
    with engine.connect() as connection:
        return {
            (row.title, row.external_id): (
                row.lifecycle_state, row.is_primary, row.is_manual, row.verified_at is not None,
            )
            for row in connection.execute(text(
                "SELECT t.local_title AS title, l.external_id, l.lifecycle_state, "
                "l.is_primary, l.is_manual, l.verified_at FROM external_title_links l "
                "JOIN catalog_titles t ON t.id = l.catalog_title_id"
            ))
        }


def _link_table(engine):
    table = ExternalTitleLink.__table__
    with engine.connect() as connection:
        return tuple(connection.execute(table.select().order_by(table.c.id)))


def _snapshot(engine):
    with engine.connect() as connection:
        return tuple(
            tuple(connection.execute(table.select().order_by(*table.primary_key.columns)))
            for table in Base.metadata.sorted_tables
        )


def test_v5_to_v6_backfills_primary_active_and_non_primary_legacy_only(tmp_path):
    engine, completion_before = _v5_database(tmp_path)
    assert migrate_schema_at_startup(engine) is True
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA user_version")) == 6 == STARTUP_COMPATIBILITY_VERSION
    column = next(
        item for item in inspect(engine).get_columns("external_title_links")
        if item["name"] == "lifecycle_state"
    )
    assert column["nullable"] is True
    assert _link_rows(engine) == {
        ("P1", "117612"): (ACTIVE, 1, 1, True),
        ("P2", "139648"): (ACTIVE, 1, 1, True),
        ("P2", "117612"): (LEGACY, 0, 1, True),
        ("P2", "555"): (LEGACY, 0, 0, False),
        ("Unlinked", "999"): (LEGACY, 0, 1, True),
    }
    states = {state for state, *_rest in _link_rows(engine).values()}
    assert SUPERSEDED not in states and UNLINKED not in states and None not in states
    with Session(engine) as db:
        assert {
            title.local_title: resolve_metadata_completion(title, title.videos).state
            for title in db.scalars(select(CatalogTitle))
        } == completion_before


def test_v6_migration_is_idempotent_and_keeps_explicit_lifecycle(tmp_path):
    engine, _ = _v5_database(tmp_path)
    assert migrate_schema_at_startup(engine) is True
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE external_title_links SET lifecycle_state = 'superseded' "
            "WHERE external_id = '999'"
        ))
    before = _snapshot(engine)
    links_before = _link_table(engine)
    assert _link_rows(engine)[("Unlinked", "999")][0] == SUPERSEDED
    writes = []

    def record(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        # Stable startup at v6 is a complete no-op.
        assert migrate_schema_at_startup(engine) is False
        assert writes == []
        assert _snapshot(engine) == before
        # The explicit full reconstruction may re-derive hierarchy, but it must
        # never touch link lifecycle again.
        migrate_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not any("external_title_links" in statement.lower() for statement in writes)
    assert _link_table(engine) == links_before


def test_v5_startup_without_null_lifecycle_rows_issues_no_update(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'v5-empty.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE external_title_links DROP COLUMN lifecycle_state"))
        connection.execute(text("PRAGMA user_version = 5"))
    statements = []

    def record(_connection, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert migrate_schema_at_startup(engine) is True
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not any(item.lstrip().upper().startswith("UPDATE") for item in statements)
    assert "lifecycle_state" in {
        column["name"] for column in inspect(engine).get_columns("external_title_links")
    }


# --- GET routes ----------------------------------------------------------------

def test_get_routes_are_no_write_and_present_only_active_link(tmp_path):
    app = create_app(Settings(
        anime_path=tmp_path / "media", database_url=f"sqlite:///{tmp_path / 'get.db'}",
        metadata_download_artwork=False, metadata_artwork_directory=tmp_path / "artwork",
    ))
    engine = app.state.sessions.kw["bind"]
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        title = add_title(db, "Realist")
        title.metadata_status = "linked_manual"
        title.metadata_record = TitleMetadata(
            display_title="Part 2", episode_count=1,
            metadata_provider="anilist", metadata_external_id="139648",
        )
        title.external_links.extend([
            link_row("139648", ACTIVE),
            link_row("117612", LEGACY),
            link_row("555", SUPERSEDED),
            link_row("777", UNLINKED),
        ])
        db.commit()
        title_id, collection_id = title.id, title.catalog_collection_id

    def endpoint(path):
        return next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)

    def request(path):
        return Request({
            "type": "http", "app": app, "method": "GET", "path": path, "root_path": "",
            "scheme": "http", "query_string": b"", "headers": [],
            "server": ("testserver", 80), "client": ("testclient", 50000),
        })

    calls = [
        ("/titles/{catalog_title_id}", (title_id,)),
        ("/metadata-review/{catalog_title_id}", (title_id,)),
        ("/metadata-review", ("all",)),
        ("/hierarchy-review/{collection_id}", (collection_id,)),
        ("/hierarchy-review", ()),
    ]
    before = _snapshot(engine)
    writes = []

    def record(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    bodies = {}
    try:
        for path, args in calls:
            response = endpoint(path)(request(path), *args)
            assert response.status_code == 200
            bodies[path] = response
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert writes == []
    assert _snapshot(engine) == before

    edit = bodies["/metadata-review/{catalog_title_id}"]
    assert edit.context["primary_external_link"].external_id == "139648"
    assert edit.context["metadata_completion"].state == "confirmed"
    detail = bodies["/hierarchy-review/{collection_id}"].body.decode()
    assert "anilist 139648" in detail
    for historical_id in ("117612", "555", "777"):
        assert f"anilist {historical_id}" not in detail
        assert f"ID {historical_id}" not in edit.body.decode()
