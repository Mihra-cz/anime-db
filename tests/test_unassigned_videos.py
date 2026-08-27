import asyncio
from pathlib import Path
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from starlette.requests import Request

from app.config import Settings
from app.database import Base
from app.hierarchy_authority import activate_manual_hierarchy_snapshot
from app.main import create_app
from app.migrations import migrate_schema
from app.models import (
    CatalogCollection, CatalogTitle, ManualSplitRuleVideo, Video, utc_now,
)
from app.scanner import scan_library
from app.unassigned_videos import (
    insufficient_video_assignment,
    insufficient_video_assignments,
)


def _app(tmp_path: Path, name: str):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / name}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
    return web_app


def _endpoint(web_app, path: str):
    return next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == path
    )


def _request(web_app, path: str) -> Request:
    return Request({
        "type": "http", "app": web_app, "method": "GET", "path": path,
        "root_path": "", "scheme": "http", "query_string": b"", "headers": [],
        "server": ("testserver", 80), "client": ("testclient", 50000),
    })


def _post_request(web_app, path: str, values: dict[str, str]) -> Request:
    body = urlencode(values).encode()
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


def _manual_title(collection: CatalogCollection, name: str, path: str, kind: str):
    title = CatalogTitle(
        collection=collection,
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=path,
        part_type=kind,
    )
    activate_manual_hierarchy_snapshot(
        title,
        part_type=kind,
        season_number=None,
        part_number=None,
        season_label=None,
        sort_order=None,
        verified_at=utc_now(),
    )
    return title


def test_insufficient_assignment_is_general_and_excludes_complete_video():
    root = Video(
        relative_path="Root Movie.mkv", root_folder=".", filename="Root Movie.mkv",
        size=1, mtime_ns=1,
    )
    holding = CatalogCollection(
        id=1, local_title="Holding", normalized_local_title="holding",
        relative_root_path="Anime/Holding",
    )
    nested = Video(
        relative_path="Anime/Holding/unknown.mkv", root_folder="Anime",
        filename="unknown.mkv", size=1, mtime_ns=1, catalog_collection=holding,
    )
    assigned_collection = CatalogCollection(
        id=2, local_title="Assigned", normalized_local_title="assigned",
        relative_root_path="Anime/Assigned",
    )
    assigned_title = CatalogTitle(
        id=3, collection=assigned_collection, local_title="Film",
        normalized_local_title="film", relative_root_path="Anime/Assigned/Film",
        part_type="film",
    )
    assigned = Video(
        relative_path="Anime/Assigned/film.mkv", root_folder="Anime",
        filename="film.mkv", size=1, mtime_ns=1,
        catalog_collection=assigned_collection, catalog_title=assigned_title,
    )
    orphan_title = CatalogTitle(
        id=4, local_title="Orphan", normalized_local_title="orphan",
        relative_root_path="@orphan/title", part_type="film",
    )
    orphan = Video(
        relative_path="Anime/orphan.mkv", root_folder="Anime",
        filename="orphan.mkv", size=1, mtime_ns=1,
        catalog_collection=holding, catalog_title=orphan_title,
    )
    technical_collection = CatalogCollection(
        id=5, local_title="Knihovna", normalized_local_title="knihovna",
        relative_root_path=".",
    )
    technical_title = CatalogTitle(
        id=6, collection=technical_collection, local_title="Knihovna",
        normalized_local_title="knihovna", relative_root_path=".", part_type="title",
    )
    technical = Video(
        relative_path="Legacy Root.mkv", root_folder=".", filename="Legacy Root.mkv",
        size=1, mtime_ns=1, catalog_collection=technical_collection,
        catalog_title=technical_title,
    )
    missing_direct_collection = Video(
        relative_path="Anime/Assigned/missing-direct.mkv", root_folder="Anime",
        filename="missing-direct.mkv", size=1, mtime_ns=1,
        catalog_title=assigned_title,
    )
    mismatched = Video(
        relative_path="Anime/Assigned/mismatch.mkv", root_folder="Anime",
        filename="mismatch.mkv", size=1, mtime_ns=1,
        catalog_collection=holding, catalog_title=assigned_title,
    )

    issues = insufficient_video_assignments([
        root, nested, assigned, orphan, technical, missing_direct_collection, mismatched,
    ])

    assert [(issue.video.filename, issue.code) for issue in issues] == [
        ("Root Movie.mkv", "missing_catalog_title"),
        ("unknown.mkv", "missing_catalog_title"),
        ("orphan.mkv", "missing_title_collection"),
        ("Legacy Root.mkv", "technical_root_placeholder"),
        ("missing-direct.mkv", "missing_video_collection"),
        ("mismatch.mkv", "collection_mismatch"),
    ]
    assert insufficient_video_assignment(assigned) is None


def test_hierarchy_overview_starts_with_blocking_unassigned_panel(tmp_path):
    web_app = _app(tmp_path, "overview.db")
    with web_app.state.sessions() as session:
        review_collection = CatalogCollection(
            local_title="Review Me", normalized_local_title="review me",
            relative_root_path="Anime/Review Me", hierarchy_status="review_required",
            hierarchy_note="Jiný hierarchy problém.",
        )
        review_title = CatalogTitle(
            collection=review_collection, local_title="Unknown Part",
            normalized_local_title="unknown part",
            relative_root_path="Anime/Review Me/Unknown Part", part_type="title",
        )
        nested = Video(
            relative_path="Anime/Review Me/unassigned.mkv", root_folder="Anime",
            filename="unassigned.mkv", size=1, mtime_ns=1,
            catalog_collection=review_collection,
        )
        root = Video(
            relative_path="Root Film.mkv", root_folder=".", filename="Root Film.mkv",
            size=1, mtime_ns=1,
        )
        assigned = Video(
            relative_path="Anime/Review Me/assigned.mkv", root_folder="Anime",
            filename="assigned.mkv", size=1, mtime_ns=1,
            catalog_collection=review_collection, catalog_title=review_title,
        )
        session.add_all([nested, root, assigned])
        session.commit()

    rendered = _endpoint(web_app, "/hierarchy-review")(
        _request(web_app, "/hierarchy-review")
    ).body.decode()

    assert "Nezařazená videa · 2" in rendered
    assert "Root Film.mkv" in rendered and "unassigned.mkv" in rendered
    assert "<strong>assigned.mkv</strong>" not in rendered.split(
        'id="unassigned-videos"', 1,
    )[1].split(
        "</section>", 1,
    )[0]
    assert 'href="/unassigned-videos">Vyřešit nezařazená videa</a>' in rendered
    assert rendered.index('id="unassigned-videos"') < rendered.index(
        'id="collection-management"'
    )
    assert rendered.index('id="unassigned-videos"') < rendered.index("Review Me")


def test_physical_root_view_is_independent_of_logical_assignment(tmp_path):
    web_app = _app(tmp_path, "physical-root.db")
    with web_app.state.sessions() as session:
        collection = CatalogCollection(
            local_title="Assigned Film", normalized_local_title="assigned film",
            relative_root_path="@manual/assigned-film",
        )
        title = _manual_title(
            collection, "Assigned Film", "@manual/assigned-film/title", "film",
        )
        assigned_root = Video(
            relative_path="Assigned Film.mkv", root_folder=".",
            filename="Assigned Film.mkv", size=1, mtime_ns=1,
            catalog_collection=collection, catalog_title=title,
        )
        unassigned_root = Video(
            relative_path="Loose Root.mkv", root_folder=".",
            filename="Loose Root.mkv", size=1, mtime_ns=1,
        )
        nested_unassigned = Video(
            relative_path="Incoming/Nested.mkv", root_folder="Incoming",
            filename="Nested.mkv", size=1, mtime_ns=1,
        )
        session.add_all([
            assigned_root,
            unassigned_root,
            nested_unassigned,
            ManualSplitRuleVideo(catalog_title=title, video=assigned_root),
        ])
        session.commit()
        assigned_id, unassigned_id = assigned_root.id, unassigned_root.id

    physical = _endpoint(web_app, "/root-videos")(
        _request(web_app, "/root-videos")
    ).body.decode()
    assigned_row = physical.split(f'id="video-{assigned_id}"', 1)[1].split(
        "</tr>", 1,
    )[0]
    unassigned_row = physical.split(f'id="video-{unassigned_id}"', 1)[1].split(
        "</tr>", 1,
    )[0]

    assert "Assigned Film.mkv" in physical
    assert "Loose Root.mkv" in physical
    assert "Nested.mkv" not in physical
    assert "LOGICKY ZAŘAZENO" in assigned_row
    assert "Assigned Film" in assigned_row and "Film" in assigned_row
    assert "Vyřešit v Hierarchy Review" not in assigned_row
    assert "NEZAŘAZENO · BLOCKING" in unassigned_row
    assert f'href="/unassigned-videos#video-{unassigned_id}"' in unassigned_row
    assert 'action="/unassigned-videos/' not in physical

    hierarchy = _endpoint(web_app, "/hierarchy-review")(
        _request(web_app, "/hierarchy-review")
    ).body.decode()
    blocker = hierarchy.split('id="unassigned-videos"', 1)[1].split(
        "</section>", 1,
    )[0]
    assert "Nezařazená videa · 2" in blocker
    assert "Loose Root.mkv" in blocker and "Nested.mkv" in blocker
    assert "Assigned Film.mkv" not in blocker

    homepage = _endpoint(web_app, "/")(_request(web_app, "/")).body.decode()
    physical_overview = homepage.split(
        'class="panel physical-folders"', 1,
    )[1].split("</section>", 1)[0]
    assert "Videa v kořeni knihovny" in physical_overview
    assert "Fyzicky v rootu: 2 · logicky zařazeno: 1 · nezařazeno: 1" in (
        physical_overview
    )

    with web_app.state.sessions() as session:
        moved = session.get(Video, assigned_id)
        moved.relative_path = "Incoming/Assigned Film.mkv"
        moved.root_folder = "Incoming"
        session.commit()

    after_physical_move = _endpoint(web_app, "/root-videos")(
        _request(web_app, "/root-videos")
    ).body.decode()
    assert "Assigned Film.mkv" not in after_physical_move
    assert "Loose Root.mkv" in after_physical_move


def test_existing_title_assignment_clears_only_unassigned_blocker(tmp_path):
    media = tmp_path / "Loose Movie.mkv"
    media.write_bytes(b"unchanged")
    before = (media.stat().st_size, media.stat().st_mtime_ns, media.read_bytes())
    web_app = _app(tmp_path, "existing-title.db")
    with web_app.state.sessions() as session:
        collection = CatalogCollection(
            local_title="Existing Anime", normalized_local_title="existing anime",
            relative_root_path="Anime/Existing Anime",
        )
        target = _manual_title(
            collection, "Film", "Anime/Existing Anime/Film", "film",
        )
        unresolved_title = CatalogTitle(
            collection=collection, local_title="Needs Review",
            normalized_local_title="needs review",
            relative_root_path="Anime/Existing Anime/Needs Review", part_type="title",
        )
        video = Video(
            relative_path=media.name, root_folder=".", filename=media.name,
            size=media.stat().st_size, mtime_ns=media.stat().st_mtime_ns,
        )
        session.add_all([target, unresolved_title, video])
        session.commit()
        video_id, title_id, collection_id = video.id, target.id, collection.id

    response = _endpoint(
        web_app, "/unassigned-videos/{video_id}/assignment",
    )(video_id, target_title_id=str(title_id), confirm_manual=True)
    assert response.status_code == 303

    with web_app.state.sessions() as session:
        stored = session.get(Video, video_id)
        collection = session.get(CatalogCollection, collection_id)
        assert insufficient_video_assignment(stored) is None
        assert stored.catalog_title_id == title_id
        assert stored.catalog_collection_id == collection_id
        assert {link.catalog_title_id for link in stored.manual_split_rule_videos} == {
            title_id
        }
        assert collection.hierarchy_status == "review_required"
    assert (media.stat().st_size, media.stat().st_mtime_ns, media.read_bytes()) == before


def test_new_title_and_new_anime_are_authoritative_and_survive_restart_rescan(
    tmp_path, monkeypatch,
):
    nested_path = tmp_path / "Incoming" / "OVA 01.mkv"
    root_path = tmp_path / "Hotarubi no Mori e.mkv"
    nested_path.parent.mkdir()
    nested_path.write_bytes(b"nested")
    root_path.write_bytes(b"root")
    physical_before = {
        path.relative_to(tmp_path).as_posix(): (
            path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes(),
        )
        for path in (nested_path, root_path)
    }
    web_app = _app(tmp_path, "new-hierarchy.db")
    engine = web_app.state.sessions.kw["bind"]
    with web_app.state.sessions() as session:
        collection = CatalogCollection(
            local_title="Existing Anime", normalized_local_title="existing anime",
            relative_root_path="Anime/Existing Anime",
        )
        existing = _manual_title(
            collection, "Season 1", "Anime/Existing Anime/Season 1", "season",
        )
        existing.season_number_manual = 1
        existing.season_label_manual = "S1"
        nested = Video(
            relative_path=nested_path.relative_to(tmp_path).as_posix(),
            root_folder="Incoming", filename=nested_path.name,
            size=nested_path.stat().st_size, mtime_ns=nested_path.stat().st_mtime_ns,
            catalog_collection=collection,
        )
        root = Video(
            relative_path=root_path.name, root_folder=".", filename=root_path.name,
            size=root_path.stat().st_size, mtime_ns=root_path.stat().st_mtime_ns,
        )
        session.add_all([existing, nested, root])
        session.commit()
        collection_id, nested_id, root_id = collection.id, nested.id, root.id

    new_title_request = _post_request(
        web_app, f"/unassigned-videos/{nested_id}/new-title", {
            "collection_id": str(collection_id),
            "local_title": "OVA – Existing Anime",
            "part_type": "ova",
            "season_number": "1",
            "season_label": "S1",
            "part_number": "",
            "sort_order": "3",
            "confirm_manual": "true",
        },
    )
    new_title_response = asyncio.run(_endpoint(
        web_app, "/unassigned-videos/{video_id}/new-title",
    )(new_title_request, nested_id))
    assert new_title_response.status_code == 303

    new_anime_request = _post_request(
        web_app, f"/unassigned-videos/{root_id}/new-anime", {
            "collection_title": "Hotarubi no Mori e",
            "local_title": "Hotarubi no Mori e",
            "part_type": "film",
            "season_number": "",
            "season_label": "",
            "part_number": "",
            "sort_order": "",
            "confirm_manual": "true",
        },
    )
    new_anime_response = asyncio.run(_endpoint(
        web_app, "/unassigned-videos/{video_id}/new-anime",
    )(new_anime_request, root_id))
    assert new_anime_response.status_code == 303

    with web_app.state.sessions() as session:
        nested = session.get(Video, nested_id)
        root = session.get(Video, root_id)
        committed = {
            nested_id: (nested.catalog_collection_id, nested.catalog_title_id),
            root_id: (root.catalog_collection_id, root.catalog_title_id),
        }
        assert nested.catalog_collection_id == collection_id
        assert nested.catalog_title.part_type_manual == "ova"
        assert nested.catalog_title.season_number_manual == 1
        assert nested.catalog_title.season_label_manual == "S1"
        assert nested.catalog_title.sort_order_manual == 3
        assert nested.catalog_title.hierarchy_manual_override is True
        assert nested.catalog_title.hierarchy_verified_at is not None
        assert root.catalog_collection.local_title == "Hotarubi no Mori e"
        assert root.catalog_title.part_type_manual == "film"
        assert root.catalog_title.season_number_manual is None
        assert all(
            insufficient_video_assignment(video) is None for video in (nested, root)
        )
        assert all(video.manual_split_rule_videos for video in (nested, root))

    overview = _endpoint(web_app, "/hierarchy-review")(
        _request(web_app, "/hierarchy-review")
    ).body.decode()
    assert 'id="unassigned-videos"' not in overview
    physical_root = _endpoint(web_app, "/root-videos")(
        _request(web_app, "/root-videos")
    ).body.decode()
    assert root_path.name in physical_root
    assert nested_path.name not in physical_root
    assert "LOGICKY ZAŘAZENO" in physical_root
    assert "Vyřešit v Hierarchy Review" not in physical_root
    homepage = _endpoint(web_app, "/")(_request(web_app, "/")).body.decode()
    assert (
        f'href="/titles/{committed[root_id][1]}">Hotarubi no Mori e</a>'
        in homepage
    )

    migrate_schema(engine)
    with web_app.state.sessions() as session:
        assert {
            video_id: (
                session.get(Video, video_id).catalog_collection_id,
                session.get(Video, video_id).catalog_title_id,
            )
            for video_id in committed
        } == committed
    restarted_root = _endpoint(web_app, "/root-videos")(
        _request(web_app, "/root-videos")
    ).body.decode()
    assert root_path.name in restarted_root and "LOGICKY ZAŘAZENO" in restarted_root

    monkeypatch.setattr(
        "app.scanner.service.probe_video",
        lambda *_args, **_kwargs: {
            "duration": 60.0, "video_codec": "h264", "width": 1920,
            "height": 1080, "audio": [], "subtitles": [],
        },
    )
    with web_app.state.sessions() as session:
        scan_library(session, tmp_path)
    with web_app.state.sessions() as session:
        reloaded = list(session.scalars(select(Video).options(
            selectinload(Video.catalog_title).selectinload(CatalogTitle.collection),
            selectinload(Video.catalog_collection),
        ).where(Video.id.in_(committed))).all())
        assert {
            video.id: (video.catalog_collection_id, video.catalog_title_id)
            for video in reloaded
        } == committed
        assert all(insufficient_video_assignment(video) is None for video in reloaded)
    rescanned_root = _endpoint(web_app, "/root-videos")(
        _request(web_app, "/root-videos")
    ).body.decode()
    assert root_path.name in rescanned_root and "LOGICKY ZAŘAZENO" in rescanned_root
    assert nested_path.name not in rescanned_root

    assert {
        path.relative_to(tmp_path).as_posix(): (
            path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes(),
        )
        for path in (nested_path, root_path)
    } == physical_before
