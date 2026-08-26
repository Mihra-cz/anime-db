from pathlib import Path

from starlette.requests import Request

from app.collection_presentation import build_collection_presentation
from app.config import Settings
from app.database import Base
from app.main import create_app
from app.models import (
    CatalogCollection,
    CatalogTitle,
    TitleMetadata,
    Video,
    utc_now,
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


def _app(tmp_path: Path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'main-presentation.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
    endpoints = {
        route.path: route.endpoint
        for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    return web_app, endpoints


def _collection(name: str) -> CatalogCollection:
    return CatalogCollection(
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=f"Anime/{name}",
        hierarchy_status="verified",
        hierarchy_verified_at=utc_now(),
    )


def _title(
    collection: CatalogCollection,
    local_title: str,
    part_type: str,
    season_number: int | None,
    filenames: tuple[str, ...],
    *,
    verified: bool = True,
    manual_order: int | None = None,
    metadata_title: str | None = None,
) -> CatalogTitle:
    path_token = f"part-{len(collection.titles) + 1}"
    title = CatalogTitle(
        collection=collection,
        local_title=local_title,
        normalized_local_title=local_title.casefold(),
        relative_root_path=f"{collection.relative_root_path}/{path_token}",
        part_type=part_type,
        season_number=season_number,
        season_label=f"S{season_number}" if season_number is not None else None,
        hierarchy_manual_override=verified,
        part_type_manual=part_type if verified else None,
        season_number_manual=season_number if verified else None,
        season_label_manual=(
            f"S{season_number}" if verified and season_number is not None else None
        ),
        sort_order_manual=manual_order,
        hierarchy_verified_at=utc_now() if verified else None,
        metadata_status="linked_manual" if metadata_title else "unlinked",
        metadata_record=(
            TitleMetadata(
                display_title=metadata_title,
                title_romaji=metadata_title,
            )
            if metadata_title else None
        ),
    )
    for index, filename in enumerate(filenames, 1):
        is_primary = part_type in {"season", "part", "cour", "title"}
        Video(
            relative_path=f"{title.relative_root_path}/{filename}",
            root_folder="Anime",
            filename=filename,
            size=1,
            mtime_ns=index,
            file_type="episode" if is_primary else "other",
            season_episode_number=index if is_primary else None,
            local_episode_number=index if is_primary else None,
            catalog_title=title,
            catalog_collection=collection,
        )
    return title


def test_view_model_uses_central_structure_and_keeps_every_title_reachable():
    collection = _collection("Structure")
    season_two = _title(collection, "Season 2", "season", 2, ())
    season_one = _title(collection, "Season 1", "season", 1, ())
    ova = _title(
        collection, "OVA – S1", "ova", 1, (), manual_order=2,
    )
    bonus = _title(
        collection, "Bonus – S1", "bonus", 1, (), manual_order=1,
    )
    anime_ova = _title(collection, "Anime OVA", "ova", None, ())
    unmatched = _title(collection, "Special – S3", "special", 3, ())
    unknown = _title(
        collection, "Unknown", "mystery", None, (), verified=False,
    )
    for title_id, title in enumerate(collection.titles, 1):
        title.id = title_id

    presentation = build_collection_presentation(reversed(collection.titles))

    assert [part.title for part in presentation.primary_parts] == [
        season_one, season_two,
    ]
    assert [
        group.part_type
        for group in presentation.primary_parts[0].supplementary_groups
    ] == ["bonus", "ova"]
    assert [part.title for part in presentation.anime_level_parts] == [
        unmatched, anime_ova, unknown,
    ]
    assert presentation.all_title_ids == {
        title.id for title in (
            season_one, season_two, ova, bonus, anime_ova, unmatched, unknown,
        ) if title.id is not None
    }
    assert presentation.direct_title is None


def test_ambiguous_same_season_primary_match_keeps_supplementary_at_anime_level():
    collection = _collection("Ambiguous")
    season = _title(collection, "Season 1", "season", 1, ())
    part = _title(collection, "Season 1 Part 2", "part", 1, ())
    ova = _title(collection, "OVA – S1", "ova", 1, ())
    for title_id, title in enumerate(collection.titles, 1):
        title.id = title_id

    presentation = build_collection_presentation(collection.titles)

    assert [item.title for item in presentation.primary_parts] == [season, part]
    assert all(not item.supplementary_groups for item in presentation.primary_parts)
    assert [item.title for item in presentation.anime_level_parts] == [ova]


def test_homepage_keeps_single_season_shortcut_with_attached_supplementary(
    tmp_path: Path,
):
    web_app, endpoints = _app(tmp_path)
    with web_app.state.sessions() as session:
        single = _collection("Single")
        single_season = _title(single, "Single Season", "season", 1, ("Single 01.mkv",))

        attached = _collection("Single Attached")
        attached_season = _title(
            attached, "Attached Season", "season", 1, ("Attached 01.mkv",),
        )
        _title(attached, "OVA – Attached", "ova", 1, ("OVA 01.mkv",))
        _title(attached, "NC – Attached", "bonus", 1, ("ED.mkv", "OP01.mkv"))

        multi = _collection("Multi")
        _title(multi, "Multi S1", "season", 1, ("Multi 01.mkv",))
        _title(multi, "Multi S2", "season", 2, ("Multi 13.mkv",))
        session.add_all((single, attached, multi))
        session.commit()
        ids = {
            "single_title": single_season.id,
            "attached_title": attached_season.id,
            "multi_collection": multi.id,
        }

    rendered = endpoints["/"](_request(web_app, "/")).body.decode()

    assert f'href="/titles/{ids["single_title"]}">Single</a>' in rendered
    assert f'href="/titles/{ids["attached_title"]}">Single Attached</a>' in rendered
    assert f'href="/collections/{ids["multi_collection"]}">Multi</a>' in rendered


def test_main_selector_and_season_detail_nest_only_exact_supplementary_context(
    tmp_path: Path,
):
    web_app, endpoints = _app(tmp_path)
    with web_app.state.sessions() as session:
        collection = _collection("Four Seasons")
        seasons = {
            number: _title(
                collection,
                f"Example S{number}",
                "season",
                number,
                (f"Example S{number}E01.mkv",),
            )
            for number in range(1, 5)
        }
        ova_s3 = _title(
            collection, "OVA – Example S3", "ova", 3, ("OVA 01.mkv",),
            metadata_title="Confirmed OVA Metadata",
        )
        special_s3 = _title(
            collection, "Specials – Example S3", "special", 3,
            ("Special 01.mkv", "Special 02.mkv"),
        )
        bonus_s3 = _title(
            collection, "NC – Example Anime", "bonus", 3,
            ("ED.mkv", "OP01.mkv", "OP02.mkv"),
        )
        _title(collection, "OVA – S1", "ova", 1, ("S1 OVA.mkv",))
        anime_level = _title(
            collection, "Anime-level OVA", "ova", None, ("Anime OVA.mkv",),
        )
        unmatched = _title(
            collection, "Special – S5", "special", 5, ("S5 Special.mkv",),
        )
        session.add(collection)
        session.commit()
        collection_id = collection.id
        season_ids = {number: title.id for number, title in seasons.items()}
        attached_ids = {ova_s3.id, special_s3.id, bonus_s3.id}
        extra_ids = {anime_level.id, unmatched.id}
        all_title_ids = {title.id for title in collection.titles}

    collection_html = endpoints["/collections/{collection_id}"](
        _request(web_app, f"/collections/{collection_id}"), collection_id,
    ).body.decode()

    for title_id in season_ids.values():
        assert f'href="/titles/{title_id}?' in collection_html
    for title_id in attached_ids:
        assert f'href="/titles/{title_id}?' not in collection_html
    for title_id in extra_ids:
        assert f'href="/titles/{title_id}?' in collection_html
    assert "Sezóny a hlavní části" in collection_html
    assert "Další části" in collection_html

    detail_html = endpoints["/titles/{catalog_title_id}"](
        _request(web_app, f"/titles/{season_ids[3]}"), season_ids[3],
    ).body.decode()
    episode_position = detail_html.index("Example S3E01.mkv")
    supplementary_position = detail_html.index("Doplňkový obsah této sezóny")
    assert episode_position < supplementary_position
    assert detail_html.count('class="season-supplementary-group"') == 3
    assert '<details class="season-supplementary-group"' in detail_html
    assert '<details class="season-supplementary-group" open' not in detail_html
    assert "Confirmed OVA Metadata" in detail_html
    assert "Specials – Example S3" in detail_html
    assert "NC – Example Anime" in detail_html
    assert "ED.mkv" in detail_html
    assert "OP01.mkv" in detail_html
    assert ">OP</a>" not in detail_html
    for title_id in attached_ids:
        assert f'href="/titles/{title_id}?' in detail_html

    bonus_html = endpoints["/titles/{catalog_title_id}"](
        _request(web_app, f"/titles/{bonus_s3.id}"), bonus_s3.id,
    ).body.decode()
    assert "NC – Example Anime" in bonus_html
    assert "OP01.mkv" in bonus_html

    review_html = endpoints["/hierarchy-review/{collection_id}"](
        _request(web_app, f"/hierarchy-review/{collection_id}"), collection_id,
    ).body.decode()
    assert review_html.count('class="panel hierarchy-title-card') == len(all_title_ids)
    assert "season-supplementary-group" not in review_html
    for title_id in all_title_ids:
        assert f'id="title-{title_id}"' in review_html


def test_single_season_detail_renders_attached_parts_without_selector_step(
    tmp_path: Path,
):
    web_app, endpoints = _app(tmp_path)
    with web_app.state.sessions() as session:
        collection = _collection("Direct Detail")
        season = _title(
            collection, "Direct Season", "season", 1, ("Direct 01.mkv",),
        )
        ova = _title(collection, "OVA – Direct", "ova", 1, ("OVA 01.mkv",))
        session.add(collection)
        session.commit()
        season_id, ova_id = season.id, ova.id

    homepage = endpoints["/"](_request(web_app, "/")).body.decode()
    assert f'href="/titles/{season_id}">Direct Detail</a>' in homepage

    detail = endpoints["/titles/{catalog_title_id}"](
        _request(web_app, f"/titles/{season_id}"), season_id,
    ).body.decode()
    assert "Direct 01.mkv" in detail
    assert "OVA – Direct" in detail
    assert f'href="/titles/{ova_id}?' in detail
