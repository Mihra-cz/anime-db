import asyncio
from html import unescape
import json
from pathlib import Path
import re
from urllib.parse import urlencode

import pytest
from sqlalchemy import select
from starlette.requests import Request

from app.config import Settings
from app.database import Base
from app.main import create_app
from app.numbering import manual_episode_number_input_value
from app.models import CatalogCollection, CatalogTitle, Video, VideoVariantGroup


def _app_with_edits(tmp_path: Path):
    app = create_app(Settings(
        anime_path=tmp_path / "media",
        database_url=f"sqlite:///{tmp_path / 'edits.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Show/Season 1",
            part_type="season", season_number=1, season_label="S1",
        )
        video = Video(
            catalog_title=title, catalog_collection=collection,
            relative_path="Anime/Show/Season 1/Show 01.mkv",
            root_folder="Anime", filename="Show 01.mkv", size=1, mtime_ns=1,
            file_type="episode", local_episode_number=1, season_episode_number=1,
        )
        first = VideoVariantGroup(catalog_title=title, manual_label="TV")
        second = VideoVariantGroup(catalog_title=title, manual_label="BD")
        session.add_all([collection, title, video, first, second])
        session.commit()
        ids = collection.id, title.id, first.id, second.id
    return app, ids


def _post_save_all(
    app, collection_id: int, payload: object, *, confirm: bool = False,
):
    values = [
        ("payload_json", json.dumps(payload)),
        ("return_to", f"/hierarchy-review/{collection_id}"),
    ]
    if confirm:
        values.append(("confirm_changes", "yes"))
    return _post_form(
        app,
        "/hierarchy-review/{collection_id}/save-all",
        f"/hierarchy-review/{collection_id}/save-all",
        values,
        collection_id,
    )


def _post_form(app, route_path: str, actual_path: str, values, *path_values):
    body = urlencode(values).encode()
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request = Request({
        "type": "http", "method": "POST", "path": actual_path,
        "raw_path": actual_path.encode(), "root_path": "", "scheme": "http",
        "query_string": b"", "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "server": ("testserver", 80), "client": ("testclient", 50000), "app": app,
    }, receive)
    endpoint = next(route.endpoint for route in app.routes if route.path == route_path)
    return asyncio.run(endpoint(request, *path_values))


def _group(group_id: int, title_id: int, label: str, source: str = ""):
    return {"kind": "variant_group", "id": group_id, "values": {
        "catalog_title_id": title_id, "manual_label": label,
        "release_source": source, "content_variant": "", "note": "",
    }}


def _visible_text(markup: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def _title_section(title_id: int, **overrides):
    values = {
        "part_type_manual": "", "season_number_manual": "",
        "season_label_manual": "", "part_number_manual": "",
        "sort_order_manual": "", "hierarchy_verified": False,
        "numbering_mode": "auto", "episode_start_offset": "",
    }
    values.update(overrides)
    return {"kind": "title_hierarchy", "id": title_id, "values": values}


def _video_section(title_id: int, video_id: int, **overrides):
    values = {
        "catalog_title_id": title_id, "content_type": "",
        "manual_episode_number": "", "media_part_number": "",
    }
    values.update(overrides)
    return {"kind": "video_hierarchy", "id": video_id, "values": values}


def test_save_all_applies_supported_edits_in_one_commit(tmp_path):
    app, (collection_id, title_id, first_id, second_id) = _app_with_edits(tmp_path)
    payload = {"sections": [
        _group(first_id, title_id, "TV revised"),
        _group(second_id, title_id, "BD revised"),
    ]}
    preview = _post_save_all(app, collection_id, payload)
    assert preview.status_code == 200, preview.body
    assert "2 neuložené změny" in preview.body.decode()
    assert "Potvrdit a uložit všechny změny" in preview.body.decode()
    with app.state.sessions() as session:
        assert session.get(VideoVariantGroup, first_id).manual_label == "TV"
        assert session.get(VideoVariantGroup, second_id).manual_label == "BD"

    response = _post_save_all(app, collection_id, payload, confirm=True)
    assert response.status_code == 303, response.body
    with app.state.sessions() as session:
        assert session.get(VideoVariantGroup, first_id).manual_label == "TV revised"
        assert session.get(VideoVariantGroup, second_id).manual_label == "BD revised"


def test_save_all_rolls_back_first_edit_when_second_fails(tmp_path):
    app, (collection_id, title_id, first_id, second_id) = _app_with_edits(tmp_path)
    response = _post_save_all(app, collection_id, {"sections": [
        _group(first_id, title_id, "TV revised"),
        _group(second_id, title_id, ""),
    ]})
    assert response.status_code == 400
    with app.state.sessions() as session:
        assert session.get(VideoVariantGroup, first_id).manual_label == "TV"
        assert session.get(VideoVariantGroup, second_id).manual_label == "BD"


def test_save_all_uses_existing_recap_validation(tmp_path):
    app, (collection_id, title_id, first_id, _) = _app_with_edits(tmp_path)
    with app.state.sessions() as session:
        recap = Video(
            catalog_title_id=title_id, catalog_collection_id=collection_id,
            relative_path="Anime/Show/Season 1/Recap 14.5.mkv",
            root_folder="Anime", filename="Recap 14.5.mkv", size=1, mtime_ns=1,
            file_type="recap",
        )
        session.add(recap)
        session.commit()
        recap_id = recap.id
    response = _post_save_all(app, collection_id, {"sections": [
        _group(first_id, title_id, "TV revised"),
        {"kind": "recap_position", "id": recap_id,
         "values": {"manual_episode_number": "14.5"}},
    ]})
    assert response.status_code == 200, response.body
    assert "Ruční pozice Recapu" in response.body.decode()
    response = _post_save_all(app, collection_id, {"sections": [
        _group(first_id, title_id, "TV revised"),
        {"kind": "recap_position", "id": recap_id,
         "values": {"manual_episode_number": "14.5"}},
    ]}, confirm=True)
    assert response.status_code == 303, response.body
    with app.state.sessions() as session:
        assert session.get(Video, recap_id).recap_episode_number_manual_tenths == 145
        assert session.get(VideoVariantGroup, first_id).manual_label == "TV revised"


def test_save_all_cannot_use_recap_section_to_edit_an_episode(tmp_path):
    app, (collection_id, title_id, first_id, _) = _app_with_edits(tmp_path)
    with app.state.sessions() as session:
        episode_id = session.scalar(select(Video.id).where(Video.catalog_title_id == title_id))
    response = _post_save_all(app, collection_id, {"sections": [
        _group(first_id, title_id, "TV revised"),
        {"kind": "recap_position", "id": episode_id,
         "values": {"manual_episode_number": "2"}},
    ]})
    assert response.status_code == 400
    with app.state.sessions() as session:
        assert session.get(VideoVariantGroup, first_id).manual_label == "TV"
        assert session.get(Video, episode_id).episode_number_manual_override is None


def test_save_all_rejects_commands_and_unknown_fields(tmp_path):
    app, (collection_id, title_id, first_id, _) = _app_with_edits(tmp_path)
    for section in (
        {"kind": "metadata_search", "id": title_id, "values": {}},
        {"kind": "scanner_rebuild", "id": title_id, "values": {}},
        {**_group(first_id, title_id, "Changed"), "url": "/metadata/search"},
    ):
        response = _post_save_all(app, collection_id, {"sections": [section]})
        assert response.status_code == 400
    with app.state.sessions() as session:
        assert session.get(VideoVariantGroup, first_id).manual_label == "TV"


def test_save_all_form_markup_excludes_confirmation_and_commands():
    source = "\n".join(
        Path(path).read_text()
        for path in (
            "app/templates/hierarchy_review_detail.html",
            "app/templates/_hierarchy_title_cards.html",
        )
    )
    script = Path("app/static/page_edit_save.js").read_text()
    hierarchy = Path("app/templates/hierarchy_edit.html").read_text()
    assert 'data-save-kind="title_hierarchy"' in hierarchy
    assert 'data-save-kind="video_hierarchy"' in hierarchy
    assert source.count('data-save-kind="variant_group"') == 1
    assert source.count('data-save-kind="recap_position"') == 1
    assert 'data-save-kind="metadata_search"' not in source
    assert 'data-save-kind="delete"' not in source
    assert 'kind === "metadata_search"' not in script
    assert 'kind === "delete"' not in script
    assert "payload_json" in script


def test_save_all_previews_title_and_two_videos_then_commits_atomically(tmp_path):
    app, (collection_id, title_id, _, _) = _app_with_edits(tmp_path)
    with app.state.sessions() as session:
        first_video = session.scalar(
            select(Video).where(Video.catalog_title_id == title_id)
        )
        second_video = Video(
            catalog_title_id=title_id, catalog_collection_id=collection_id,
            relative_path="Anime/Show/Season 1/Show Special 2.mkv",
            root_folder="Anime", filename="Show Special 2.mkv",
            size=2, mtime_ns=2, file_type="special",
        )
        session.add(second_video)
        session.commit()
        first_id, second_id = first_video.id, second_video.id
    payload = {"sections": [
        _title_section(title_id, numbering_mode="season_local"),
        _video_section(title_id, first_id, manual_episode_number="3"),
        _video_section(
            title_id, second_id, content_type="special",
            manual_episode_number="1", media_part_number="2",
        ),
    ]}

    preview = _post_save_all(app, collection_id, payload)
    rendered = preview.body.decode()
    assert preview.status_code == 200, rendered
    assert "3 neuložené změny" in rendered
    assert "Struktura části" in rendered
    assert "Show 01.mkv" in rendered
    assert "Show Special 2.mkv" in rendered
    assert "Současná effective identita: S01E01" in rendered
    assert "Výsledná effective identita: S01E03" in rendered
    assert "Výsledná effective identita: Special 01" in rendered
    with app.state.sessions() as session:
        assert session.get(CatalogTitle, title_id).numbering_mode == "unknown"
        assert session.get(Video, first_id).episode_number_manual_override is None
        assert session.get(Video, second_id).content_type_manual is None

    saved = _post_save_all(app, collection_id, payload, confirm=True)
    assert saved.status_code == 303
    with app.state.sessions() as session:
        assert session.get(CatalogTitle, title_id).numbering_mode == "season_local"
        assert session.get(Video, first_id).episode_number_manual_override == 3
        assert session.get(Video, second_id).content_type_manual == "special"
        assert session.get(Video, second_id).episode_number_manual_override == 1
        assert session.get(Video, second_id).media_part_number == 2


def test_save_all_rolls_back_title_and_first_video_when_later_video_is_invalid(
    tmp_path,
):
    app, (collection_id, title_id, _, _) = _app_with_edits(tmp_path)
    with app.state.sessions() as session:
        first = session.scalar(select(Video).where(Video.catalog_title_id == title_id))
        second = Video(
            catalog_title_id=title_id, catalog_collection_id=collection_id,
            relative_path="Anime/Show/Season 1/Show 02.mkv",
            root_folder="Anime", filename="Show 02.mkv", size=2, mtime_ns=2,
            file_type="episode", local_episode_number=2, season_episode_number=2,
        )
        session.add(second)
        session.commit()
        first_id, second_id = first.id, second.id
    payload = {"sections": [
        _title_section(title_id, episode_start_offset="2"),
        _video_section(title_id, first_id, manual_episode_number="3"),
        _video_section(title_id, second_id, media_part_number="invalid"),
    ]}

    response = _post_save_all(app, collection_id, payload, confirm=True)
    rendered = response.body.decode()
    assert response.status_code == 400
    assert "Show 02.mkv" in rendered
    assert "Část média musí být kladné celé číslo" in rendered
    assert "Žádná část dávky nebyla uložena" in rendered
    with app.state.sessions() as session:
        assert session.get(CatalogTitle, title_id).episode_start_offset is None
        assert session.get(Video, first_id).episode_number_manual_override is None
        assert session.get(Video, second_id).media_part_number is None


def test_metadata_settings_edit_previews_then_atomically_saves(tmp_path):
    app, (_, title_id, _, _) = _app_with_edits(tmp_path)
    path = f"/metadata-review/{title_id}/edit"
    values = [
        ("manual_display_title", "Show · Ručně"), ("requirement", "required"),
        ("return_to", f"/metadata-review/{title_id}#metadata"),
    ]
    preview = _post_form(
        app, "/metadata-review/{catalog_title_id}/edit", path, values, title_id,
    )
    assert preview.status_code == 200
    assert "Zobrazovaný název" in preview.body.decode()
    assert "Požadavek na metadata" in preview.body.decode()
    with app.state.sessions() as session:
        title = session.get(CatalogTitle, title_id)
        assert title.manual_display_title is None
        assert title.metadata_requirement_manual is None
    saved = _post_form(
        app, "/metadata-review/{catalog_title_id}/edit", path,
        values + [("confirm_changes", "yes")], title_id,
    )
    assert saved.status_code == 303
    with app.state.sessions() as session:
        title = session.get(CatalogTitle, title_id)
        assert title.manual_display_title == "Show · Ručně"
        assert title.metadata_requirement_manual == "required"


def test_metadata_settings_validation_preserves_input_and_marks_dirty_section(
    tmp_path,
):
    app, (_, title_id, _, _) = _app_with_edits(tmp_path)
    response = _post_form(
        app,
        "/metadata-review/{catalog_title_id}/edit",
        f"/metadata-review/{title_id}/edit",
        [
            ("manual_display_title", "Uživatelská hodnota"),
            ("requirement", "invalid"),
        ],
        title_id,
    )

    assert response.status_code == 400
    rendered = response.body.decode()
    assert "Požadavek „invalid“ není podporovaný" in rendered
    assert 'value="Uživatelská hodnota"' in rendered
    assert '<option value="invalid" selected>' in rendered
    assert 'data-dirty-on-load="true"' in rendered
    with app.state.sessions() as session:
        title = session.get(CatalogTitle, title_id)
        assert title.manual_display_title is None
        assert title.metadata_requirement_manual is None


def test_hierarchy_video_edit_rolls_back_earlier_axis_on_later_failure(tmp_path):
    app, (collection_id, title_id, _, _) = _app_with_edits(tmp_path)
    with app.state.sessions() as session:
        video = session.scalar(select(Video).where(Video.catalog_title_id == title_id))
        video_id = video.id
    video_path = (
        f"/hierarchy-review/{collection_id}/titles/{title_id}/videos/{video_id}/edit"
    )
    values = [
        ("manual_episode_number", ""), ("media_part_number", "invalid"),
        ("content_type", "bonus"),
        ("confirm_changes", "yes"),
    ]
    response = _post_form(
        app,
        "/hierarchy-review/{collection_id}/titles/{catalog_title_id}/videos/{video_id}/edit",
        video_path, values, collection_id, title_id, video_id,
    )
    assert response.status_code == 400
    rendered = response.body.decode()
    assert "Část média musí být kladné celé číslo" in rendered
    assert 'name="media_part_number" value="invalid"' in rendered
    assert 'data-dirty-on-load="true"' in rendered
    with app.state.sessions() as session:
        video = session.get(Video, video_id)
        assert video.media_part_number is None
        assert video.content_type_manual is None


def test_hierarchy_video_edit_previews_then_saves_multiple_axes(tmp_path):
    app, (collection_id, title_id, _, _) = _app_with_edits(tmp_path)
    with app.state.sessions() as session:
        video = session.scalar(select(Video).where(Video.catalog_title_id == title_id))
        video_id = video.id
        episode = str(manual_episode_number_input_value(video) or "")
    path = (
        f"/hierarchy-review/{collection_id}/titles/{title_id}/videos/{video_id}/edit"
    )
    values = [
        ("manual_episode_number", "1"), ("media_part_number", "1"),
        ("content_type", "bonus"),
        ("return_to", f"/hierarchy-review/{collection_id}/titles/{title_id}#hierarchy-video-{video_id}"),
    ]
    route = (
        "/hierarchy-review/{collection_id}/titles/{catalog_title_id}/videos/{video_id}/edit"
    )
    preview = _post_form(
        app, route, path, values, collection_id, title_id, video_id,
    )
    assert preview.status_code == 200
    rendered = preview.body.decode()
    assert "Video: Show 01.mkv" in rendered
    assert "Typ obsahu: Epizoda → Bonus" in rendered
    assert "Ordinal Bonusu: automaticky → 1" in rendered
    assert "Část média: neurčeno → 1" in rendered
    assert "Současná effective identita: S01E01" in rendered
    assert "Výsledná effective identita: Bonus 01" in rendered
    with app.state.sessions() as session:
        video = session.get(Video, video_id)
        assert video.media_part_number is None
        assert video.content_type_manual is None
    saved = _post_form(
        app, route, path, values + [("confirm_changes", "yes")],
        collection_id, title_id, video_id,
    )
    assert saved.status_code == 303
    with app.state.sessions() as session:
        video = session.get(Video, video_id)
        assert video.media_part_number == 1
        assert video.content_type_manual == "bonus"


def test_hierarchy_edit_restores_identity_numbering_and_contextual_labels(tmp_path):
    app, (collection_id, title_id, first_group_id, _) = _app_with_edits(tmp_path)
    with app.state.sessions() as session:
        episode = session.scalar(select(Video).where(Video.catalog_title_id == title_id))
        episode.absolute_episode_number = 1
        episode.external_episode_number = 1
        episode.episode_number_source = "manual"
        episode.media_part_number = 1
        episode.video_variant_group_id = first_group_id
        special = Video(
            catalog_title_id=title_id, catalog_collection_id=collection_id,
            relative_path="Anime/Show/Season 1/Show Special 2.mkv",
            root_folder="Anime", filename="Show Special 2.mkv",
            size=2, mtime_ns=2, file_type="special",
            content_type_manual="special", episode_number_manual_override=2,
        )
        recap = Video(
            catalog_title_id=title_id, catalog_collection_id=collection_id,
            relative_path="Anime/Show/Season 1/Show Recap 1.5.mkv",
            root_folder="Anime", filename="Show Recap 1.5.mkv",
            size=3, mtime_ns=3, file_type="recap",
            content_type_manual="recap",
            recap_episode_number_manual_tenths=15,
        )
        suggested = Video(
            catalog_title_id=title_id, catalog_collection_id=collection_id,
            relative_path="Anime/Show/Season 1/Show OVA 3.mkv",
            root_folder="Anime", filename="Show OVA 3.mkv",
            size=4, mtime_ns=4, file_type="ova",
        )
        session.add_all([special, recap, suggested])
        session.commit()

    endpoint = next(
        route.endpoint for route in app.routes
        if getattr(route, "path", "")
        == "/hierarchy-review/{collection_id}/titles/{catalog_title_id}"
    )
    path = f"/hierarchy-review/{collection_id}/titles/{title_id}"
    request = Request({
        "type": "http", "method": "GET", "path": path,
        "root_path": "", "scheme": "http", "query_string": b"",
        "headers": [], "server": ("testserver", 80),
        "client": ("testclient", 50000), "app": app,
    })
    rendered = _visible_text(endpoint(request, collection_id, title_id).body.decode())

    assert "S01E01" in rendered
    assert "Lokální číslo: 1" in rendered
    assert "Absolutní číslo: 1" in rendered
    assert "Externí číslo: 1" in rendered
    assert "Zdroj číslování: manual" in rendered
    assert "Efektivní typ: Epizoda" in rendered
    assert "Raw/parser typ: episode" in rendered
    assert "Část média 1" in rendered
    assert "Současná varianta: TV" in rendered
    assert "Ruční číslo epizody" in rendered
    assert "Lokální supplementary identita: Special 02" in rendered
    assert "Ordinal Specialu" in rendered
    assert "Lokální supplementary identita: Recap 1.5" in rendered
    assert "Ruční pozice Recapu" in rendered
    assert "A/B marker, varianta a Část média zůstávají samostatné osy" in rendered
    assert "Existující návrh:" in rendered
    assert "Návrh je pouze evidence; nic se bez potvrzení nezmění." in rendered


@pytest.mark.parametrize("part_type", ["season", "movie", "bonus", "ova", "special"])
def test_each_catalog_title_type_uses_readonly_detail_and_scoped_editors(
    tmp_path, part_type,
):
    app, (collection_id, title_id, _, _) = _app_with_edits(tmp_path)
    with app.state.sessions() as session:
        title = session.get(CatalogTitle, title_id)
        title.part_type = part_type
        session.commit()
    endpoints = {
        route.path: route.endpoint for route in app.routes
        if hasattr(route, "endpoint")
    }
    def get(path, route, *args):
        request = Request({
            "type": "http", "method": "GET", "path": path,
            "root_path": "", "scheme": "http", "query_string": b"",
            "headers": [], "server": ("testserver", 80),
            "client": ("testclient", 50000), "app": app,
        })
        return endpoints[route](request, *args).body.decode()

    detail = get(
        f"/titles/{title_id}", "/titles/{catalog_title_id}", title_id,
    )
    assert f'href="/hierarchy-review/{collection_id}/titles/{title_id}"' in detail
    assert f'href="/metadata-review/{title_id}"' in detail
    assert f'href="/media-check/titles/{title_id}"' in detail
    assert 'name="manual_display_title"' not in detail
    assert 'name="manual_episode_number"' not in detail
    assert 'name="manual_language"' not in detail
    assert '>Uložit<' not in detail

    hierarchy = get(
        f"/hierarchy-review/{collection_id}/titles/{title_id}",
        "/hierarchy-review/{collection_id}/titles/{catalog_title_id}",
        collection_id, title_id,
    )
    metadata = get(
        f"/metadata-review/{title_id}",
        "/metadata-review/{catalog_title_id}", title_id,
    )
    media = get(
        f"/media-check/titles/{title_id}",
        "/media-check/titles/{catalog_title_id}", title_id,
    )
    assert 'name="part_type_manual"' in hierarchy
    assert 'name="content_type"' in hierarchy
    assert 'name="media_part_number"' in hierarchy
    assert 'name="manual_display_title"' not in hierarchy
    assert 'name="manual_display_title"' in metadata
    assert 'name="metadata_query"' in metadata
    assert 'name="content_type"' not in metadata
    assert f'action="/media-check/titles/{title_id}/bulk-edit"' in media
    assert 'name="hardsub"' in media
    assert 'name="metadata_query"' not in media


def test_shared_dirty_state_uses_real_disabled_buttons_and_error_fallback():
    script = Path("app/static/local_edit_dirty.js").read_text()

    assert "button.disabled = !changed" in script
    assert "form.dataset.dirtyOnLoad === 'true'" in script
    assert 'data-edit-form' in Path("app/templates/media_edit.html").read_text()
