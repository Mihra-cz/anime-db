import re
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import Settings
from app.database import Base
from app.main import create_app
from app.models import CatalogCollection, CatalogTitle, Video


def _request(web_app) -> Request:
    return Request({
        "type": "http",
        "app": web_app,
        "method": "GET",
        "path": "/hierarchy-review",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })


def _add_collection(
    session: Session,
    *,
    local_title: str,
    display_title: str,
    status: str = "automatic",
    filenames: tuple[str, ...] = ("Episode 01.mkv",),
    file_type: str = "episode",
    direct_root_title: bool = False,
) -> CatalogCollection:
    path = f"Anime/{local_title}"
    collection = CatalogCollection(
        local_title=local_title,
        normalized_local_title=local_title.casefold(),
        relative_root_path=path,
        manual_display_title=display_title,
        hierarchy_status=status,
    )
    title = CatalogTitle(
        collection=collection,
        local_title=local_title,
        normalized_local_title=local_title.casefold(),
        relative_root_path=path if direct_root_title else f"{path}/Season 1",
        part_type="season" if file_type == "episode" else "special",
        season_number=1 if file_type == "episode" else None,
        season_label="S1" if file_type == "episode" else None,
    )
    for number, filename in enumerate(filenames, start=1):
        video = Video(
            catalog_collection=collection,
            catalog_title=title,
            relative_path=f"{path}/{filename}",
            root_folder="Anime",
            filename=filename,
            size=number,
            mtime_ns=number,
            file_type=file_type,
        )
        if file_type == "episode":
            video.local_episode_number = number
            video.season_episode_number = number
            video.absolute_episode_number = number
    session.add(collection)
    return collection


@pytest.fixture
def hierarchy_index_app(tmp_path):
    database_path = tmp_path / "hierarchy-index.db"
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{database_path}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    engine = web_app.state.sessions.kw["bind"]
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        verified = _add_collection(
            session,
            local_title="Z-local-verified",
            display_title="Alpha Verified",
            status="verified",
        )
        automatic = _add_collection(
            session,
            local_title="Y-local-automatic",
            display_title="Bravo Automatic",
        )
        review = _add_collection(
            session,
            local_title="X-local-review",
            display_title="Charlie Review",
            status="review_required",
        )
        supplementary = _add_collection(
            session,
            local_title="W-local-supplementary",
            display_title="Delta Supplementary",
            status="verified",
            filenames=("NCOP.mkv", "NCOP clean.mkv"),
            file_type="ncop",
        )
        resolved = _add_collection(
            session,
            local_title="V-local-resolved",
            display_title="Echo Resolved",
            filenames=("NCOP01.mkv", "NCOP02.mkv"),
            file_type="ncop",
        )
        long_set = _add_collection(
            session,
            local_title="U-local-long",
            display_title="Foxtrot Long",
            filenames=tuple(f"Episode {number:02}.mkv" for number in range(1, 16)),
            direct_root_title=True,
        )
        direct_collection = CatalogCollection(
            local_title="T-local-direct",
            normalized_local_title="t-local-direct",
            relative_root_path="Anime/T-local-direct",
            manual_display_title="Golf Direct Assignment",
        )
        Video(
            catalog_collection=direct_collection,
            relative_path="Anime/T-local-direct/Unassigned part.mkv",
            root_folder="Anime",
            filename="Unassigned part.mkv",
            size=1,
            mtime_ns=1,
            file_type="episode",
        )

        empty_title_collection = CatalogCollection(
            local_title="Empty title placeholder",
            normalized_local_title="empty title placeholder",
            relative_root_path="Anime/Empty title placeholder",
        )
        empty_title_collection.titles.append(CatalogTitle(
            local_title="Empty title placeholder",
            normalized_local_title="empty title placeholder",
            relative_root_path="Anime/Empty title placeholder/Season 1",
        ))
        ghost_collection = CatalogCollection(
            local_title="Ghost collection",
            normalized_local_title="ghost collection",
            relative_root_path="Anime/Ghost collection",
        )
        technical_root = CatalogCollection(
            local_title="Technical root",
            normalized_local_title="technical root",
            relative_root_path=".",
        )
        technical_title = CatalogTitle(
            collection=technical_root,
            local_title="Technical root",
            normalized_local_title="technical root",
            relative_root_path=".",
            part_type="season",
            season_number=1,
            season_label="S1",
        )
        Video(
            catalog_collection=technical_root,
            catalog_title=technical_title,
            relative_path="Loose.mkv",
            root_folder=".",
            filename="Loose.mkv",
            size=1,
            mtime_ns=1,
            file_type="episode",
            local_episode_number=1,
            season_episode_number=1,
            absolute_episode_number=1,
        )
        session.add_all([
            empty_title_collection,
            ghost_collection,
            technical_root,
            direct_collection,
        ])
        session.commit()
        ids = {
            "verified": verified.id,
            "automatic": automatic.id,
            "review": review.id,
            "supplementary": supplementary.id,
            "resolved": resolved.id,
            "long": long_set.id,
            "direct": direct_collection.id,
        }
    return web_app, ids


def _render_index(web_app) -> str:
    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review"
    )
    response = endpoint(_request(web_app), message=None)
    assert response.status_code == 200
    return response.body.decode()


def _all_index(rendered: str) -> str:
    return rendered.split(
        '<details class="panel all-collections-index" id="all-collections">', 1
    )[1]


def _queue(rendered: str) -> str:
    return rendered.split('id="hierarchy-review-queue"', 1)[1].split(
        '<details class="panel all-collections-index"', 1
    )[0]


def test_all_relevant_collections_are_in_all_anime(hierarchy_index_app):
    web_app, _ids = hierarchy_index_app
    index = _all_index(_render_index(web_app))

    assert re.findall(r'data-collection-name="([^"]+)"', index) == [
        "Alpha Verified",
        "Bravo Automatic",
        "Charlie Review",
        "Delta Supplementary",
        "Echo Resolved",
        "Foxtrot Long",
        "Golf Direct Assignment",
    ]
    assert "Všechna anime (7)" in index


def test_empty_placeholders_and_technical_root_are_not_in_all_anime(
    hierarchy_index_app,
):
    web_app, _ids = hierarchy_index_app
    index = _all_index(_render_index(web_app))

    assert "Empty title placeholder" not in index
    assert "Ghost collection" not in index
    assert "Technical root" not in index


@pytest.mark.parametrize(
    ("name", "status_key", "label"),
    (
        ("Alpha Verified", "verified", "Ověřeno"),
        ("Bravo Automatic", "automatic_ok", "Automaticky OK"),
        ("Charlie Review", "review_required", "Vyžaduje kontrolu"),
        ("Foxtrot Long", "long_episode_set", "Zvláštně dlouhá sada epizod"),
    ),
)
def test_all_anime_badges_use_shared_precedence(
    hierarchy_index_app, name, status_key, label,
):
    web_app, _ids = hierarchy_index_app
    index = _all_index(_render_index(web_app))
    row = re.search(
        rf'<li class="all-collections-row" data-collection-name="{name}">(.*?)</li>',
        index,
    ).group(1)

    assert f'data-hierarchy-status="{status_key}"' in row
    assert f">{label}</span>" in row


@pytest.mark.parametrize("stored_status", ("verified", "automatic"))
def test_supplementary_ordinal_review_overrides_stored_clean_badge(
    hierarchy_index_app, stored_status,
):
    web_app, ids = hierarchy_index_app
    engine = web_app.state.sessions.kw["bind"]
    with Session(engine) as session:
        collection = session.get(CatalogCollection, ids["supplementary"])
        collection.hierarchy_status = stored_status
        session.commit()
    index = _all_index(_render_index(web_app))
    row = re.search(
        r'data-collection-name="Delta Supplementary">(.*?)</li>', index
    ).group(1)

    assert 'data-hierarchy-status="review_required"' in row
    assert "Vyžaduje kontrolu" in row


def test_resolved_collection_remains_in_index_and_only_changes_badge(
    hierarchy_index_app,
):
    web_app, _ids = hierarchy_index_app
    rendered = _render_index(web_app)
    index = _all_index(rendered)
    queue = _queue(rendered)

    assert 'data-collection-name="Echo Resolved"' in index
    resolved_row = re.search(
        r'data-collection-name="Echo Resolved">(.*?)</li>', index
    ).group(1)
    assert 'data-hierarchy-status="automatic_ok"' in resolved_row
    assert "Echo Resolved" not in queue


def test_each_index_row_links_directly_to_hierarchy_review_detail(
    hierarchy_index_app,
):
    web_app, ids = hierarchy_index_app
    index = _all_index(_render_index(web_app))

    for collection_id in ids.values():
        assert f'href="/hierarchy-review/{collection_id}"' in index
    assert 'href="/collections/' not in index
    assert 'href="/titles/' not in index


def test_work_queue_keeps_existing_membership_while_index_includes_it_too(
    hierarchy_index_app,
):
    web_app, ids = hierarchy_index_app
    rendered = _render_index(web_app)
    queue = _queue(rendered)
    index = _all_index(rendered)

    assert f'href="/hierarchy-review/{ids["review"]}"' in queue
    assert f'href="/hierarchy-review/{ids["supplementary"]}"' in queue
    assert "Chybějící supplementary ordinal" in queue
    for key in ("verified", "automatic", "resolved", "long", "direct"):
        assert f'href="/hierarchy-review/{ids[key]}"' not in queue
    assert f'href="/hierarchy-review/{ids["review"]}"' in index
    assert f'href="/hierarchy-review/{ids["supplementary"]}"' in index


def test_all_anime_is_closed_by_default_and_rows_only_contain_name_and_badge(
    hierarchy_index_app,
):
    web_app, _ids = hierarchy_index_app
    rendered = _render_index(web_app)
    opening_tag = rendered.split('id="all-collections"', 1)[0].rsplit("<", 1)[1]
    index = _all_index(rendered)

    assert " open" not in opening_tag
    row_contents = re.findall(
        r'<li class="all-collections-row"[^>]*>(.*?)</li>', index
    )
    assert row_contents
    assert all(content.count("<a ") == 1 for content in row_contents)
    assert all(content.count("<span ") == 1 for content in row_contents)
    assert all("<small" not in content for content in row_contents)


def test_client_search_filters_display_names_case_insensitively():
    template = Path("app/templates/hierarchy_review.html").read_text(encoding="utf-8")

    assert 'id="all-collections-search" type="search"' in template
    assert 'aria-controls="all-collections-list"' in template
    assert 'data-collection-name="{{ row.display_title }}"' in template
    assert ".toLocaleLowerCase('cs-CZ')" in template
    assert ".includes(query)" in template
    assert "row.hidden = !matches" in template
    assert "search.addEventListener('input', filterRows)" in template


def test_hidden_search_result_overrides_flex_row_display():
    template = Path("app/templates/hierarchy_review.html").read_text(encoding="utf-8")
    css = Path("app/static/style.css").read_text(encoding="utf-8")
    flex_rule = re.search(r"\.all-collections-row \{([^}]*)\}", css).group(1)
    hidden_rule = re.search(
        r"\.all-collections-row\[hidden\] \{([^}]*)\}", css
    ).group(1)

    assert "row.hidden = !matches" in template
    assert "display: flex" in flex_rule
    assert "display: none" in hidden_rule
    assert "!important" not in hidden_rule
    assert css.index(".all-collections-row[hidden]") > css.index(
        ".all-collections-row {"
    )
