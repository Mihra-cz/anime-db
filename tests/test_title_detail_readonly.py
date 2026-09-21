import json
from pathlib import Path
import re

from starlette.requests import Request

from app.config import Settings
from app.database import Base
from app.main import create_app
from app.models import (
    Artwork, CatalogCollection, CatalogTitle, ExternalTitleLink, TitleMetadata,
    Video, utc_now,
)


def _title_detail_app(
    tmp_path: Path, *, confirmed: bool, with_artwork: bool = False,
    with_original: bool = True, with_thumbnail: bool = True,
):
    artwork_root = tmp_path / "artwork"
    app = create_app(Settings(
        anime_path=tmp_path / "media",
        database_url=f"sqlite:///{tmp_path / 'title-detail.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=artwork_root,
    ))
    with app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Catalog Show", normalized_local_title="catalog show",
            relative_root_path="Anime/Catalog Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Catalog Show Season 1",
            normalized_local_title="catalog show season 1",
            relative_root_path="Anime/Catalog Show/Season 1",
            part_type="season", season_number=1, season_label="S1",
            metadata_status="linked_manual" if confirmed else "linked_auto",
            metadata_record=TitleMetadata(
                display_title="Catalog Show Display",
                title_romaji="Catalog Show Romaji",
                title_english="Catalog Show English",
                title_native="カタログショー",
                synonyms_json=json.dumps(["Catalog Alias", "Second Alias"]),
                release_year=2024, season="SPRING", format="TV",
                status="FINISHED", episode_count=12,
                episode_duration_minutes=24,
                genres_json=json.dumps(["Drama", "Fantasy"]),
                tags_json=json.dumps(["Found Family", "Magic"]),
                country_of_origin="JP",
                description="A confirmed catalog synopsis.",
                metadata_provider="anilist", metadata_external_id="4242",
                cover_image_url="https://remote.invalid/cover.jpg",
            ),
        )
        title.external_links.append(ExternalTitleLink(
            provider="anilist", external_id="4242",
            external_url="https://anilist.co/anime/4242",
            match_method="manual_search", is_primary=True,
            is_manual=confirmed, verified_at=utc_now() if confirmed else None,
        ))
        title.videos.append(Video(
            catalog_collection=collection,
            relative_path="Anime/Catalog Show/Season 1/01.mkv",
            root_folder="Anime", filename="01.mkv", size=1, mtime_ns=1,
            file_type="episode", local_episode_number=1,
            season_episode_number=1,
        ))
        if with_artwork:
            original_path = "anilist/4242/cover-original.jpg"
            thumbnail_path = "anilist/4242/cover-thumb.jpg"
            thumbnail = artwork_root / thumbnail_path
            thumbnail.parent.mkdir(parents=True, exist_ok=True)
            if with_thumbnail:
                thumbnail.write_bytes(b"cached thumbnail")
            if with_original:
                (artwork_root / original_path).write_bytes(b"cached original")
            title.artwork.append(Artwork(
                provider="anilist", external_id="4242", artwork_type="cover",
                remote_url="https://remote.invalid/cover.jpg",
                local_path=original_path,
                thumbnail_path=thumbnail_path, mime_type="image/jpeg",
                file_size=16, is_primary=True,
            ))
        session.add(collection)
        session.commit()
        ids = collection.id, title.id
    return app, ids


def _render_title_detail(app, title_id: int) -> str:
    endpoint = next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None) == "/titles/{catalog_title_id}"
    )
    path = f"/titles/{title_id}"
    request = Request({
        "type": "http", "app": app, "method": "GET", "path": path,
        "root_path": "", "scheme": "http", "query_string": b"",
        "headers": [], "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })
    return endpoint(request, title_id).body.decode()


def test_readonly_title_detail_presents_confirmed_metadata_cached_artwork_and_compact_pills(
    tmp_path,
):
    app, (collection_id, title_id) = _title_detail_app(
        tmp_path, confirmed=True, with_artwork=True,
    )

    rendered = _render_title_detail(app, title_id)

    for expected in (
        "Catalog Show Display", "Catalog Show Romaji", "Catalog Show English",
        "カタログショー", "Catalog Alias", "Second Alias", "2024",
        "SPRING", "TV", "FINISHED", "12", "24 min", "Drama", "Fantasy",
        "Found Family", "Magic", "JP", "A confirmed catalog synopsis.",
        "AniList", "4242",
    ):
        assert expected in rendered
    assert 'src="/artwork/anilist/4242/cover-original.jpg"' in rendered
    assert 'src="/artwork/anilist/4242/cover-thumb.jpg"' not in rendered
    assert "https://remote.invalid/cover.jpg" not in rendered
    assert 'href="https://anilist.co/anime/4242"' in rendered

    assert 'class="title-authority-links"' in rendered
    assert rendered.count('class="status-badge authority-status-pill ') == 3
    assert f'href="/hierarchy-review/{collection_id}/titles/{title_id}"' in rendered
    assert f'href="/metadata-review/{title_id}"' in rendered
    assert f'href="/media-check/titles/{title_id}"' in rendered
    assert "Struktura, číslování, varianty a duplicity" not in rendered
    assert "Provider, kandidáti, artwork a requirement" not in rendered
    assert "Audio, titulky, hardsub a dostupnost" not in rendered
    assert "authority-status-card" not in rendered

    assert 'name="manual_display_title"' not in rendered
    assert 'name="metadata_query"' not in rendered
    assert 'name="manual_episode_number"' not in rendered
    assert ">Uložit<" not in rendered


def test_readonly_title_detail_falls_back_to_thumbnail_when_original_is_missing(
    tmp_path,
):
    app, (_, title_id) = _title_detail_app(
        tmp_path, confirmed=True, with_artwork=True, with_original=False,
    )

    rendered = _render_title_detail(app, title_id)

    assert 'src="/artwork/anilist/4242/cover-thumb.jpg"' in rendered
    assert 'class="title-artwork-placeholder"' not in rendered


def test_readonly_title_detail_uses_placeholder_when_cached_files_are_missing(
    tmp_path,
):
    app, (_, title_id) = _title_detail_app(
        tmp_path, confirmed=True, with_artwork=True,
        with_original=False, with_thumbnail=False,
    )

    rendered = _render_title_detail(app, title_id)

    assert 'class="title-artwork-placeholder"' in rendered
    assert "Obal není uložen." in rendered
    assert "https://remote.invalid/cover.jpg" not in rendered


def test_readonly_title_detail_uses_stable_artwork_fallback_without_remote_image(
    tmp_path,
):
    app, (_, title_id) = _title_detail_app(
        tmp_path, confirmed=True, with_artwork=False,
    )
    with app.state.sessions() as session:
        metadata = session.get(TitleMetadata, title_id)
        metadata.metadata_provider = None
        metadata.metadata_external_id = None
        session.commit()

    rendered = _render_title_detail(app, title_id)

    assert "Obal není uložen." in rendered
    assert 'class="title-artwork-placeholder"' in rendered
    assert "https://remote.invalid/cover.jpg" not in rendered
    assert "Catalog Show Romaji" in rendered
    assert "Zdroj: <strong>AniList</strong> · ID 4242" in rendered
    assert ">None<" not in rendered


def test_readonly_title_detail_hides_unconfirmed_metadata_content(tmp_path):
    app, (_, title_id) = _title_detail_app(
        tmp_path, confirmed=False, with_artwork=False,
    )

    rendered = _render_title_detail(app, title_id)
    metadata_section = rendered.split(
        '<section class="panel title-catalog-metadata"', 1,
    )[1].split('<nav class="title-authority-links"', 1)[0]

    assert "Potvrzená metadata nejsou dostupná." in rendered
    assert "Catalog Show Display" not in metadata_section
    assert "Catalog Show Romaji" not in metadata_section
    assert "A confirmed catalog synopsis." not in metadata_section
    assert "https://anilist.co/anime/4242" not in metadata_section
    assert "Obal není uložen." in rendered
    assert not re.search(r'action="[^"]*/metadata/', rendered)
