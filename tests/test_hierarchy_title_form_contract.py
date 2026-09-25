"""Form contract of the Hierarchy Edit title section.

``hierarchy_fields.js`` hides and disables the conditional structural inputs a
part type does not use, so the browser omits them from both the local submit
and the Save All payload.  These tests submit the rendered form the way the
browser does and verify persisted state from a fresh session.
"""
import asyncio
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from starlette.requests import Request

from app.config import Settings
from app.database import Base
from app.hierarchy_evaluation import finalize_hierarchy_write
from app.hierarchy_types import PART_TYPE_CHOICES
from app.main import create_app
from app.models import CatalogCollection, CatalogTitle, Video, utc_now


TITLE_ROUTE = "/hierarchy-review/{collection_id}/titles/{catalog_title_id}"
TITLE_EDIT_ROUTE = f"{TITLE_ROUTE}/edit"
SAVE_ALL_ROUTE = "/hierarchy-review/{collection_id}/save-all"
INCOMPLETE = "formulář není úplný"
UNSUPPORTED = "nepodporované nebo duplicitní pole"
CONDITIONAL = ("season_number_manual", "season_label_manual", "part_number_manual")
STRUCTURE_LABELS = (
    "Typ části:", "Číslo sezóny:", "Označení sezóny:", "Číslo Part:",
    "Ruční pořadí:", "Zařazení ověřeno:",
)

# What hierarchy_fields.js leaves visible (and therefore submitted) per part
# type; tests/js/test_hierarchy_title_form.js pins the script to this table.
BROWSER_VISIBLE = {
    "": set(),
    "season": set(CONDITIONAL),
    "part": {"season_number_manual", "part_number_manual"},
    **{
        value: {"season_number_manual", "season_label_manual"}
        for value, _ in PART_TYPE_CHOICES if value not in {"season", "part"}
    },
}


# --- fixtures -------------------------------------------------------------------

def _app(tmp_path: Path):
    app = create_app(Settings(
        anime_path=tmp_path / "media",
        database_url=f"sqlite:///{tmp_path / 'title-form.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
    return app


def _seed(app, name: str, titles, *, filename):
    """titles: (folder, season, source numbers, manual snapshot or None)."""
    with app.state.sessions() as session:
        collection = CatalogCollection(
            local_title=name, normalized_local_title=name.casefold(),
            relative_root_path=name,
        )
        ids = []
        for folder, season, sources, manual in titles:
            title = CatalogTitle(
                collection=collection, local_title=folder,
                normalized_local_title=folder.casefold(),
                relative_root_path=f"{name}/{folder}", part_type="season",
                season_number=season, season_label=f"S{season}",
                **(manual or {}),
            )
            for number in sources:
                file = filename(number)
                session.add(Video(
                    catalog_title=title, catalog_collection=collection,
                    relative_path=f"{name}/{folder}/{file}", root_folder=name,
                    filename=file, size=1, mtime_ns=1, file_type="episode",
                ))
            ids.append(title)
        session.add(collection)
        session.flush()
        finalize_hierarchy_write([collection])
        session.commit()
        return collection.id, [title.id for title in ids]


def _manual(part_type, season, *, part=None, label=None):
    return {
        "hierarchy_manual_override": True, "part_type_manual": part_type,
        "season_number_manual": season, "part_number_manual": part,
        "season_label_manual": label or f"S{season}",
        "hierarchy_verified_at": utc_now(),
    }


def _hundred_man(app, s2_manual=None):
    return _seed(app, "100-man no Inochi no Ue ni Ore wa Tatteiru (P20-L21)", (
        ("Season 1 (P20)", 1, range(1, 13), None),
        ("Season 2 (L21)", 2, range(13, 25), s2_manual),
    ), filename=lambda n: f"100-man no Inochi no Ue ni Ore wa Tatteiru - {n:02}.mkv")


def _konosuba(app):
    return _seed(app, "Kono Subarashii Sekai ni Shukufuku wo!", (
        ("Season 1", 1, range(1, 11), None),
        ("Season 2", 2, range(11, 21), None),
    ), filename=lambda n: f"Kono Subarashii Sekai ni Shukufuku wo!{n:02}.mp4")


def _show(app, s2_manual):
    return _seed(app, "Show", (
        ("Season 1", 1, range(1, 4), _manual("season", 1)),
        ("Season 2", 2, range(4, 7), s2_manual),
    ), filename=lambda n: f"Show - {n:02}.mkv")


# --- request helpers --------------------------------------------------------------

def _call(app, route: str, method: str, path: str, values, *path_values):
    body = urlencode(values).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request = Request({
        "type": "http", "method": method, "path": path,
        "raw_path": path.encode(), "root_path": "", "scheme": "http",
        "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "server": ("testserver", 80), "client": ("testclient", 50000),
        "app": app,
    }, receive)
    endpoint = next(
        item.endpoint for item in app.routes
        if getattr(item, "path", None) == route
        and method in getattr(item, "methods", ())
    )
    result = endpoint(request, *path_values)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


class _TitleFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.fields: list[tuple[str, str]] = []
        self.select: str | None = None
        self.options: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form" and "hierarchy-title-editor" in (attributes.get("class") or ""):
            self.inside = True
        elif not self.inside:
            return
        elif tag == "input" and attributes.get("type") == "checkbox":
            if "checked" in attributes:
                self.fields.append((attributes["name"], attributes["value"]))
        elif tag == "input":
            self.fields.append((attributes["name"], attributes.get("value") or ""))
        elif tag == "select":
            self.select, self.options = attributes["name"], []
        elif tag == "option" and self.select:
            self.options.append((attributes.get("value") or "", "selected" in attributes))

    def handle_endtag(self, tag):
        if tag == "select" and self.select:
            chosen = next((value for value, on in self.options if on), self.options[0][0])
            self.fields.append((self.select, chosen))
            self.select = None
        elif tag == "form" and self.inside:
            self.inside = False


def _rendered_form(app, collection_id: int, title_id: int) -> list[tuple[str, str]]:
    response = _call(
        app, TITLE_ROUTE, "GET", f"/hierarchy-review/{collection_id}/titles/{title_id}",
        [], collection_id, title_id,
    )
    parser = _TitleFormParser()
    parser.feed(response.body.decode())
    assert parser.fields, "title form was not rendered"
    return parser.fields


def _browser_submit(app, collection_id, title_id, *, javascript=True, **changes):
    """Edit the rendered title form and serialize it like the browser would."""
    fields = dict(_rendered_form(app, collection_id, title_id))
    for name, value in changes.items():
        if name == "hierarchy_verified":
            fields.pop(name, None)
            if value:
                fields[name] = "true"
        else:
            fields[name] = value
    if javascript:
        visible = BROWSER_VISIBLE[fields["part_type_manual"]]
        fields = {
            name: value for name, value in fields.items()
            if name not in CONDITIONAL or name in visible
        }
    return list(fields.items())


def _post_title(app, collection_id, title_id, values, *, confirm=False):
    values = list(values) + ([("confirm_changes", "yes")] if confirm else [])
    return _call(
        app, TITLE_EDIT_ROUTE, "POST",
        f"/hierarchy-review/{collection_id}/titles/{title_id}/edit",
        values, collection_id, title_id,
    )


def _save_all_section(title_id: int, submitted):
    """Serialize a title form exactly like page_edit_save.js."""
    fields = dict(submitted)
    values = {
        name: value for name, value in fields.items()
        if name not in {"hierarchy_verified", "return_to"}
    }
    values["hierarchy_verified"] = "hierarchy_verified" in fields
    return {"kind": "title_hierarchy", "id": title_id, "values": values}


def _post_save_all(app, collection_id, sections, *, confirm=False):
    values = [
        ("payload_json", json.dumps({"sections": sections})),
        ("return_to", f"/hierarchy-review/{collection_id}"),
    ]
    if confirm:
        values.append(("confirm_changes", "yes"))
    return _call(
        app, SAVE_ALL_ROUTE, "POST", f"/hierarchy-review/{collection_id}/save-all",
        values, collection_id,
    )


def _text(response) -> str:
    return response.body.decode()


def _structure(app, title_id):
    with app.state.sessions() as session:
        title = session.get(CatalogTitle, title_id)
        return {
            "part_type_manual": title.part_type_manual,
            "season_number_manual": title.season_number_manual,
            "season_label_manual": title.season_label_manual,
            "part_number_manual": title.part_number_manual,
            "sort_order_manual": title.sort_order_manual,
            "hierarchy_manual_override": title.hierarchy_manual_override,
            "hierarchy_verified_at": title.hierarchy_verified_at,
            "effective": (
                title.effective_part_type, title.effective_season_number,
                title.effective_part_number,
            ),
        }


def _numbering(app, title_id):
    with app.state.sessions() as session:
        title = session.get(CatalogTitle, title_id)
        videos = sorted(title.videos, key=lambda video: video.filename)
        return {
            "mode": title.numbering_mode,
            "offset": title.episode_start_offset,
            "manual": title.numbering_manual,
            "canonical": [video.season_episode_number for video in videos],
            "absolute": [video.absolute_episode_number for video in videos],
        }


def _assert_only_numbering_changes(rendered: str):
    for label in STRUCTURE_LABELS:
        assert label not in rendered, label


# --- contract ---------------------------------------------------------------------

@pytest.mark.parametrize("part_type", sorted(BROWSER_VISIBLE))
def test_backend_relevance_matches_the_fields_the_browser_submits(part_type):
    from app.page_edit_save import title_hierarchy_conditional_fields

    assert title_hierarchy_conditional_fields(part_type) == BROWSER_VISIBLE[part_type]


def test_rendered_title_form_uses_the_shared_conditional_markers():
    template = Path("app/templates/hierarchy_edit.html").read_text()
    for marker, name in (
        ("data-season-number", "season_number_manual"),
        ("data-season-label", "season_label_manual"),
        ("data-part-number", "part_number_manual"),
    ):
        assert f'<label {marker}>' in template
        assert f'name="{name}"' in template


# --- 100-man S2: automatic hierarchy, numbering-only ------------------------------------

def test_automatic_title_numbering_only_local_save_previews_then_confirms(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _hundred_man(app)
    structure_before = _structure(app, s2)
    numbering_before = _numbering(app, s2)
    submitted = _browser_submit(
        app, collection_id, s2, numbering_mode="absolute", episode_start_offset="12",
    )
    assert {name for name, _ in submitted}.isdisjoint(CONDITIONAL)

    preview = _post_title(app, collection_id, s2, submitted)
    rendered = _text(preview)
    assert preview.status_code == 200, rendered
    assert INCOMPLETE not in rendered
    assert "Režim číslování: auto → absolute" in rendered
    assert "Počet předchozích epizod: neurčeno → 12" in rendered
    _assert_only_numbering_changes(rendered)
    assert _structure(app, s2) == structure_before
    assert _numbering(app, s2) == numbering_before

    saved = _post_title(app, collection_id, s2, submitted, confirm=True)
    assert saved.status_code == 303, _text(saved)
    assert _structure(app, s2) == structure_before
    after = _numbering(app, s2)
    assert (after["mode"], after["offset"], after["manual"]) == ("absolute", 12, True)
    assert after["canonical"] == list(range(1, 13))
    assert after["absolute"] == list(range(13, 25))


def test_automatic_title_numbering_only_save_all_previews_then_confirms(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _hundred_man(app)
    structure_before = _structure(app, s2)
    numbering_before = _numbering(app, s2)
    section = _save_all_section(s2, _browser_submit(
        app, collection_id, s2, numbering_mode="absolute", episode_start_offset="12",
    ))
    assert set(section["values"]).isdisjoint(CONDITIONAL)

    preview = _post_save_all(app, collection_id, [section])
    rendered = _text(preview)
    assert preview.status_code == 200, rendered
    assert "Režim číslování: auto → absolute" in rendered
    assert "Počet předchozích epizod: neurčeno → 12" in rendered
    _assert_only_numbering_changes(rendered)
    assert _structure(app, s2) == structure_before
    assert _numbering(app, s2) == numbering_before

    saved = _post_save_all(app, collection_id, [section], confirm=True)
    assert saved.status_code == 303, _text(saved)
    assert _structure(app, s2) == structure_before
    after = _numbering(app, s2)
    assert (after["mode"], after["offset"]) == ("absolute", 12)
    assert after["canonical"] == list(range(1, 13))
    assert after["absolute"] == list(range(13, 25))


def test_automatic_title_offset_only_change(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _hundred_man(app)
    structure_before = _structure(app, s2)
    submitted = _browser_submit(app, collection_id, s2, episode_start_offset="12")

    preview = _post_title(app, collection_id, s2, submitted)
    rendered = _text(preview)
    assert preview.status_code == 200, rendered
    assert "Počet předchozích epizod: neurčeno → 12" in rendered
    assert "Režim číslování:" not in rendered
    _assert_only_numbering_changes(rendered)

    assert _post_title(app, collection_id, s2, submitted, confirm=True).status_code == 303
    after = _numbering(app, s2)
    assert (after["mode"], after["offset"], after["manual"]) == ("unknown", 12, True)
    assert _structure(app, s2) == structure_before


def test_konosuba_s2_automatic_hierarchy_absolute_offset_ten(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _konosuba(app)
    structure_before = _structure(app, s2)
    submitted = _browser_submit(
        app, collection_id, s2, numbering_mode="absolute", episode_start_offset="10",
    )

    assert _post_title(app, collection_id, s2, submitted).status_code == 200
    saved = _post_title(app, collection_id, s2, submitted, confirm=True)
    assert saved.status_code == 303, _text(saved)
    assert _structure(app, s2) == structure_before
    after = _numbering(app, s2)
    assert (after["mode"], after["offset"]) == ("absolute", 10)
    assert after["canonical"] == list(range(1, 11))
    assert after["absolute"] == list(range(11, 21))


def test_numbering_only_edit_keeps_an_incomplete_historical_snapshot(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _hundred_man(app, s2_manual={
        "season_number_manual": 2, "season_label_manual": "S2",
        "hierarchy_verified_at": utc_now(),
    })
    before = _structure(app, s2)
    assert before["part_type_manual"] is None and before["season_number_manual"] == 2
    submitted = _browser_submit(
        app, collection_id, s2, numbering_mode="absolute", episode_start_offset="12",
    )

    preview = _post_title(app, collection_id, s2, submitted)
    _assert_only_numbering_changes(_text(preview))
    assert _post_title(app, collection_id, s2, submitted, confirm=True).status_code == 303
    section = _save_all_section(s2, _browser_submit(
        app, collection_id, s2, episode_start_offset="13",
    ))
    assert _post_save_all(app, collection_id, [section], confirm=True).status_code == 303
    assert _structure(app, s2) == before
    assert _numbering(app, s2)["offset"] == 13


# --- concrete types, numbering-only --------------------------------------------------

@pytest.mark.parametrize("via_save_all", [False, True])
def test_season_title_numbering_only_does_not_rewrite_structure(tmp_path, via_save_all):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, _manual("season", 2, label="Final Season"))
    before = _structure(app, s2)
    submitted = _browser_submit(
        app, collection_id, s2, numbering_mode="season_local", episode_start_offset="3",
    )
    assert {"season_number_manual", "season_label_manual", "part_number_manual"} <= dict(submitted).keys()

    if via_save_all:
        section = _save_all_section(s2, submitted)
        preview = _post_save_all(app, collection_id, [section])
        saved = _post_save_all(app, collection_id, [section], confirm=True)
    else:
        preview = _post_title(app, collection_id, s2, submitted)
        saved = _post_title(app, collection_id, s2, submitted, confirm=True)
    assert preview.status_code == 200, _text(preview)
    _assert_only_numbering_changes(_text(preview))
    assert saved.status_code == 303, _text(saved)
    assert _structure(app, s2) == before
    assert _numbering(app, s2)["mode"] == "season_local"


@pytest.mark.parametrize("via_save_all", [False, True])
def test_part_title_numbering_only_does_not_rewrite_structure(tmp_path, via_save_all):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, _manual("part", 2, part=2))
    before = _structure(app, s2)
    assert before["season_label_manual"] == "S2"
    submitted = _browser_submit(app, collection_id, s2, episode_start_offset="3")
    assert "season_label_manual" not in dict(submitted)

    if via_save_all:
        section = _save_all_section(s2, submitted)
        preview = _post_save_all(app, collection_id, [section])
        saved = _post_save_all(app, collection_id, [section], confirm=True)
    else:
        preview = _post_title(app, collection_id, s2, submitted)
        saved = _post_title(app, collection_id, s2, submitted, confirm=True)
    assert preview.status_code == 200, _text(preview)
    _assert_only_numbering_changes(_text(preview))
    assert saved.status_code == 303, _text(saved)
    assert _structure(app, s2) == before
    assert _numbering(app, s2)["offset"] == 3


# --- structural transitions ------------------------------------------------------------

def test_season_to_automatic_clears_the_whole_manual_snapshot(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, _manual("season", 2, label="Final Season"))
    submitted = _browser_submit(
        app, collection_id, s2, part_type_manual="", hierarchy_verified=False,
    )
    assert {name for name, _ in submitted}.isdisjoint(CONDITIONAL)

    preview = _post_title(app, collection_id, s2, submitted)
    rendered = _text(preview)
    assert preview.status_code == 200, rendered
    assert "Typ části: season → neurčeno" in rendered
    assert "Číslo sezóny: 2 → neurčeno" in rendered
    assert "Označení sezóny: Final Season → neurčeno" in rendered
    assert "Zařazení ověřeno: ano → ne" in rendered

    assert _post_title(app, collection_id, s2, submitted, confirm=True).status_code == 303
    after = _structure(app, s2)
    assert {key: after[key] for key in after if key != "effective"} == {
        "part_type_manual": None, "season_number_manual": None,
        "season_label_manual": None, "part_number_manual": None,
        "sort_order_manual": None, "hierarchy_manual_override": False,
        "hierarchy_verified_at": None,
    }


def test_season_to_automatic_still_requires_clearing_verification(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, _manual("season", 2))
    before = _structure(app, s2)
    submitted = _browser_submit(app, collection_id, s2, part_type_manual="")

    response = _post_title(app, collection_id, s2, submitted, confirm=True)
    assert response.status_code == 400
    assert "zvolte konkrétní typ části" in _text(response)
    assert _structure(app, s2) == before


def test_no_javascript_season_to_automatic_with_stale_values_is_rejected(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, _manual("season", 2))
    before = _structure(app, s2)
    submitted = _browser_submit(
        app, collection_id, s2, javascript=False,
        part_type_manual="", hierarchy_verified=False,
    )
    assert dict(submitted)["season_number_manual"] == "2"

    response = _post_title(app, collection_id, s2, submitted, confirm=True)
    assert response.status_code == 400
    assert "zvolte konkrétní typ části" in _text(response)
    assert _structure(app, s2) == before


def test_season_to_part_does_not_keep_the_hidden_season_label(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, _manual("season", 2, label="Final Season"))
    submitted = _browser_submit(
        app, collection_id, s2, part_type_manual="part", part_number_manual="1",
    )
    assert "season_label_manual" not in dict(submitted)

    preview = _post_title(app, collection_id, s2, submitted)
    rendered = _text(preview)
    assert preview.status_code == 200, rendered
    assert "Typ části: season → part" in rendered
    assert "Číslo Part: neurčeno → 1" in rendered
    assert "Označení sezóny: Final Season → neurčeno" in rendered

    assert _post_title(app, collection_id, s2, submitted, confirm=True).status_code == 303
    after = _structure(app, s2)
    assert after["part_type_manual"] == "part"
    assert (after["season_number_manual"], after["part_number_manual"]) == (2, 1)
    assert after["season_label_manual"] == "S2"
    assert after["effective"] == ("part", 2, 1)


def test_part_to_season_uses_the_now_visible_fields(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, _manual("part", 2, part=1))
    submitted = _browser_submit(
        app, collection_id, s2, part_type_manual="season", part_number_manual="",
    )
    assert dict(submitted)["season_label_manual"] == "S2"

    assert _post_title(app, collection_id, s2, submitted).status_code == 200
    assert _post_title(app, collection_id, s2, submitted, confirm=True).status_code == 303
    after = _structure(app, s2)
    assert after["part_type_manual"] == "season"
    assert (after["season_number_manual"], after["part_number_manual"]) == (2, None)
    assert after["season_label_manual"] == "S2"
    assert after["hierarchy_manual_override"] is True


@pytest.mark.parametrize("via_save_all", [False, True])
def test_structure_and_numbering_combined_edit_is_atomic(tmp_path, via_save_all):
    app = _app(tmp_path)
    collection_id, (_, s2) = _hundred_man(app)
    changes = {
        "part_type_manual": "season", "season_number_manual": "2",
        "hierarchy_verified": True, "numbering_mode": "absolute",
        "episode_start_offset": "12",
    }
    submitted = _browser_submit(app, collection_id, s2, **changes)
    if via_save_all:
        saved = _post_save_all(
            app, collection_id, [_save_all_section(s2, submitted)], confirm=True,
        )
    else:
        saved = _post_title(app, collection_id, s2, submitted, confirm=True)
    assert saved.status_code == 303, _text(saved)
    after = _structure(app, s2)
    assert (after["part_type_manual"], after["season_number_manual"]) == ("season", 2)
    assert after["hierarchy_manual_override"] is True
    numbering = _numbering(app, s2)
    assert (numbering["mode"], numbering["offset"]) == ("absolute", 12)
    assert numbering["absolute"] == list(range(13, 25))


@pytest.mark.parametrize("via_save_all", [False, True])
def test_confirm_failure_rolls_back_structure_and_numbering(tmp_path, via_save_all):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, _manual("season", 2))
    structure_before = _structure(app, s2)
    numbering_before = _numbering(app, s2)
    submitted = _browser_submit(
        app, collection_id, s2, sort_order_manual="5",
        numbering_mode="absolute", episode_start_offset="nope",
    )
    if via_save_all:
        response = _post_save_all(
            app, collection_id, [_save_all_section(s2, submitted)], confirm=True,
        )
    else:
        response = _post_title(app, collection_id, s2, submitted, confirm=True)
    assert response.status_code == 400
    assert "Offset musí být nezáporné celé číslo" in _text(response)
    assert _structure(app, s2) == structure_before
    assert _numbering(app, s2) == numbering_before


# --- still rejected ------------------------------------------------------------------------

@pytest.mark.parametrize(("manual", "part_type", "dropped"), [
    (_manual("season", 2), "season", "season_number_manual"),
    (_manual("season", 2), "season", "season_label_manual"),
    (_manual("season", 2), "season", "part_number_manual"),
    (_manual("part", 2, part=1), "part", "part_number_manual"),
    (_manual("part", 2, part=1), "part", "season_number_manual"),
    (_manual("season", 2), "film", "season_label_manual"),
    (None, "", "numbering_mode"),
    (None, "", "episode_start_offset"),
    (None, "", "sort_order_manual"),
    (None, "", "part_type_manual"),
])
def test_missing_relevant_field_is_still_rejected(tmp_path, manual, part_type, dropped):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, manual)
    structure_before = _structure(app, s2)
    numbering_before = _numbering(app, s2)
    submitted = [
        item for item in _browser_submit(
            app, collection_id, s2, part_type_manual=part_type,
            hierarchy_verified=bool(part_type), episode_start_offset="3",
        )
        if item[0] != dropped
    ]
    response = _post_title(app, collection_id, s2, submitted, confirm=True)
    assert response.status_code == 400
    assert INCOMPLETE in _text(response)

    section = _save_all_section(s2, submitted)
    response = _post_save_all(app, collection_id, [section], confirm=True)
    assert response.status_code == 400
    assert "Neplatná pole editační sekce" in _text(response)
    assert _structure(app, s2) == structure_before
    assert _numbering(app, s2) == numbering_before


@pytest.mark.parametrize("extra", [
    ("catalog_title_id", "1"), ("numbering_mode", "absolute"),
    ("season_number_manual", "2"),
])
def test_unsupported_or_duplicate_field_is_still_rejected(tmp_path, extra):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, _manual("season", 2))
    structure_before = _structure(app, s2)
    submitted = _browser_submit(app, collection_id, s2, episode_start_offset="3")
    response = _post_title(app, collection_id, s2, submitted + [extra], confirm=True)
    assert response.status_code == 400
    assert UNSUPPORTED in _text(response)
    assert _structure(app, s2) == structure_before
    assert _numbering(app, s2)["offset"] is None


def test_save_all_rejects_unknown_title_field(tmp_path):
    app = _app(tmp_path)
    collection_id, (_, s2) = _hundred_man(app)
    section = _save_all_section(s2, _browser_submit(
        app, collection_id, s2, episode_start_offset="12",
    ))
    section["values"]["catalog_title_id"] = "1"
    response = _post_save_all(app, collection_id, [section], confirm=True)
    assert response.status_code == 400
    assert _numbering(app, s2)["offset"] is None


# --- progressive fallback -------------------------------------------------------------------

@pytest.mark.parametrize("manual", [None, _manual("part", 2, part=2)])
def test_no_javascript_numbering_only_submit_still_works(tmp_path, manual):
    app = _app(tmp_path)
    collection_id, (_, s2) = _show(app, manual)
    before = _structure(app, s2)
    submitted = _browser_submit(
        app, collection_id, s2, javascript=False, episode_start_offset="3",
    )
    assert set(CONDITIONAL) <= dict(submitted).keys()

    preview = _post_title(app, collection_id, s2, submitted)
    assert preview.status_code == 200, _text(preview)
    _assert_only_numbering_changes(_text(preview))
    assert _post_title(app, collection_id, s2, submitted, confirm=True).status_code == 303
    assert _structure(app, s2) == before
    assert _numbering(app, s2)["offset"] == 3
