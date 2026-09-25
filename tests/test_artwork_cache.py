from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.metadata import artwork as artwork_module
from app.metadata.artwork import (
    ArtworkCacheError,
    cache_cover,
    collection_artwork_thumbnail_url,
    local_artwork_original_url,
    local_artwork_thumbnail_url,
    primary_cover_artwork,
)
from datetime import datetime, timezone

from app.models import Artwork, CatalogCollection, CatalogTitle, ExternalTitleLink


def image_bytes(fmt):
    output = BytesIO()
    Image.new("RGB", (800, 600), "purple").save(output, format=fmt)
    return output.getvalue()


def setup(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    title = CatalogTitle(local_title="Show", normalized_local_title="show", relative_root_path="Anime/Show")
    session.add(title)
    session.flush()
    return engine, session, title, tmp_path / "artwork"


def client(body, content_type, *, content_length=None):
    def handler(request):
        headers = {"content-type": content_type}
        if content_length is not None:
            headers["content-length"] = str(content_length)
        return httpx.Response(200, content=body, headers=headers, request=request)
    return httpx.Client(transport=httpx.MockTransport(handler))


class RecordingClient(httpx.Client):
    def __init__(self, transport):
        super().__init__(transport=transport)
        self.request_timeout = None

    def stream(self, method, url, **kwargs):
        self.request_timeout = kwargs.get("timeout")
        return super().stream(method, url, **kwargs)


def stored_artwork(
    external_id, thumbnail_path, *, primary=True, artwork_type="cover",
):
    return Artwork(
        provider="anilist",
        external_id=str(external_id),
        artwork_type=artwork_type,
        remote_url=f"https://img/{external_id}",
        local_path=f"anilist/{external_id}/cover-original.jpg",
        thumbnail_path=thumbnail_path,
        mime_type="image/jpeg",
        file_size=1,
        is_primary=primary,
    )


def test_collection_artwork_uses_primary_cover_from_main_title_order(tmp_path):
    root = tmp_path / "artwork"
    main_thumbnail = root / "anilist" / "2" / "cover-thumb.webp"
    supplementary_thumbnail = root / "anilist" / "1" / "cover-thumb.webp"
    main_thumbnail.parent.mkdir(parents=True)
    supplementary_thumbnail.parent.mkdir(parents=True)
    main_thumbnail.write_bytes(b"main")
    supplementary_thumbnail.write_bytes(b"supplementary")
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    supplementary = CatalogTitle(
        id=1, collection=collection, local_title="OVA",
        normalized_local_title="ova", relative_root_path="Anime/Show/OVA",
        part_type="ova", artwork=[stored_artwork(
            1, "anilist/1/cover-thumb.webp",
        )],
    )
    main = CatalogTitle(
        id=2, collection=collection, local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season",
        season_number=1, artwork=[
            stored_artwork(
                "ignored", "anilist/ignored/cover-thumb.webp", primary=False,
            ),
            stored_artwork(2, "anilist/2/cover-thumb.webp"),
        ],
    )

    assert primary_cover_artwork(main).external_id == "2"
    assert collection_artwork_thumbnail_url(
        [supplementary, main], root,
    ) == "/artwork/anilist/2/cover-thumb.webp"


def test_local_artwork_url_rejects_missing_and_unsafe_thumbnail_paths(tmp_path):
    root = tmp_path / "artwork"

    assert local_artwork_thumbnail_url(
        stored_artwork(1, "anilist/1/missing.webp"), root,
    ) is None
    assert local_artwork_thumbnail_url(
        stored_artwork(2, "../outside.webp"), root,
    ) is None


def test_local_artwork_original_url_uses_only_existing_safe_local_file(tmp_path):
    root = tmp_path / "artwork"
    original = root / "anilist" / "1" / "cover-original.jpg"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original")

    assert local_artwork_original_url(
        stored_artwork(1, "anilist/1/cover-thumb.webp"), root,
    ) == "/artwork/anilist/1/cover-original.jpg"
    assert local_artwork_original_url(
        stored_artwork(2, "anilist/2/cover-thumb.webp"), root,
    ) is None

    unsafe = stored_artwork(3, "anilist/3/cover-thumb.webp")
    unsafe.local_path = "../outside.jpg"
    assert local_artwork_original_url(unsafe, root) is None


@pytest.mark.parametrize(("mime", "fmt", "suffix"), [
    ("image/jpeg", "JPEG", ".jpg"), ("image/png", "PNG", ".png"),
    ("image/webp", "WEBP", ".webp"),
])
def test_cache_accepts_supported_images_and_creates_thumbnail(tmp_path, mime, fmt, suffix):
    engine, session, title, root = setup(tmp_path)
    result = cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="1",
                         remote_url="https://img/cover", root=root, client=client(image_bytes(fmt), mime))
    assert result.local_path.endswith(suffix)
    assert (root / result.local_path).is_file()
    assert (root / result.thumbnail_path).is_file()
    with Image.open(root / result.thumbnail_path) as thumb:
        assert thumb.width == 400
    session.close(); engine.dispose()


def test_cache_rejects_html_and_oversized_response(tmp_path):
    engine, session, title, root = setup(tmp_path)
    with pytest.raises(ArtworkCacheError):
        cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="1",
                    remote_url="https://img/cover", root=root, client=client(b"<html>", "text/html"))
    with pytest.raises(ArtworkCacheError):
        cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="1",
                    remote_url="https://img/cover", root=root, max_bytes=10,
                    client=client(image_bytes("JPEG"), "image/jpeg", content_length=100))
    assert session.scalar(select(Artwork)) is None
    session.close(); engine.dispose()


def test_cache_publishes_with_atomic_replace(tmp_path, monkeypatch):
    engine, session, title, root = setup(tmp_path)
    calls = []
    real_replace = artwork_module.os.replace
    monkeypatch.setattr(artwork_module.os, "replace", lambda source, target: (calls.append((Path(source), Path(target))), real_replace(source, target))[1])
    cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="1",
                remote_url="https://img/cover", root=root, client=client(image_bytes("JPEG"), "image/jpeg"))
    assert len(calls) == 2
    assert all(source.name.startswith(".cover-") for source, _ in calls)
    session.close(); engine.dispose()


def test_old_image_survives_failure_and_is_replaced_only_after_success(tmp_path):
    engine, session, title, root = setup(tmp_path)
    first = cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="1",
                        remote_url="https://img/old", root=root, client=client(image_bytes("JPEG"), "image/jpeg"))
    old_path = root / first.local_path
    old_bytes = old_path.read_bytes()
    with pytest.raises(ArtworkCacheError):
        cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="1",
                    remote_url="https://img/broken", root=root, client=client(b"html", "text/html"))
    assert old_path.read_bytes() == old_bytes
    second = cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="1",
                         remote_url="https://img/new", root=root, client=client(image_bytes("PNG"), "image/png"))
    assert second.remote_url == "https://img/new"
    assert (root / second.local_path).suffix == ".png"
    assert not old_path.exists()
    session.close(); engine.dispose()


def test_artwork_download_uses_explicit_split_timeout(tmp_path):
    engine, session, title, root = setup(tmp_path)
    body = image_bytes("JPEG")
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=body, headers={"content-type": "image/jpeg"}, request=request
    ))
    recording = RecordingClient(transport)
    cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="1",
                remote_url="https://img/cover", root=root, timeout_seconds=15, client=recording)
    assert isinstance(recording.request_timeout, httpx.Timeout)
    assert recording.request_timeout.read == 15
    assert recording.request_timeout.connect == 5
    recording.close(); session.close(); engine.dispose()


# --- presentation cover follows the current metadata authority ------------------

def confirmed_link(external_id, *, lifecycle="active"):
    active = lifecycle == "active"
    return ExternalTitleLink(
        provider="anilist", external_id=str(external_id), match_method="manual_search",
        is_primary=active, is_manual=True, verified_at=datetime.now(timezone.utc),
        lifecycle_state=lifecycle,
    )


def reconfirmed_title(artwork):
    """A title re-linked from a historical identity (117612) to a new one (139648)."""
    return CatalogTitle(
        id=248, local_title="Part 2", normalized_local_title="part 2",
        relative_root_path="Anime/Show/Part 2", part_type="season",
        external_links=[
            confirmed_link(117612, lifecycle="legacy_historical"), confirmed_link(139648),
        ],
        artwork=artwork,
    )


def test_confirmed_identity_cover_wins_over_a_stale_historical_primary(tmp_path):
    root = tmp_path / "artwork"
    for external_id in (117612, 139648):
        thumbnail = root / "anilist" / str(external_id) / "cover-thumb.webp"
        thumbnail.parent.mkdir(parents=True)
        thumbnail.write_bytes(str(external_id).encode())
    historical = stored_artwork(117612, "anilist/117612/cover-thumb.webp")
    current = stored_artwork(139648, "anilist/139648/cover-thumb.webp")
    title = reconfirmed_title([historical, current])

    assert primary_cover_artwork(title) is current
    assert collection_artwork_thumbnail_url(
        [title], root,
    ) == "/artwork/anilist/139648/cover-thumb.webp"
    # The historical row stays preserved evidence; nothing is rewritten on read.
    assert historical.is_primary is True and current.is_primary is True
    assert [link.lifecycle_state for link in title.external_links] == [
        "legacy_historical", "active",
    ]


def test_confirmed_identity_never_falls_back_to_a_historical_cover():
    title = reconfirmed_title([stored_artwork(117612, "anilist/117612/cover-thumb.webp")])
    assert primary_cover_artwork(title) is None


def test_title_without_confirmed_metadata_keeps_the_primary_fallback():
    primary = stored_artwork(7, "anilist/7/cover-thumb.webp")
    title = CatalogTitle(
        local_title="Show", normalized_local_title="show", relative_root_path="Anime/Show",
        artwork=[stored_artwork(6, "anilist/6/cover-thumb.webp", primary=False), primary],
    )
    assert primary_cover_artwork(title) is primary
    title.artwork = [stored_artwork(6, "anilist/6/cover-thumb.webp", primary=False)]
    assert primary_cover_artwork(title) is None


def test_caching_a_cover_keeps_a_single_primary_and_preserves_old_rows(tmp_path):
    engine, session, title, root = setup(tmp_path)
    title.external_links.extend([
        confirmed_link(1, lifecycle="legacy_historical"), confirmed_link(2),
    ])
    session.flush()
    links_before = [
        (link.external_id, link.lifecycle_state, link.is_primary)
        for link in title.external_links
    ]
    first = cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="1",
                        remote_url="https://img/1", root=root, client=client(image_bytes("JPEG"), "image/jpeg"))
    second = cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="2",
                         remote_url="https://img/2", root=root, client=client(image_bytes("JPEG"), "image/jpeg"))
    session.commit()

    with Session(engine) as fresh:
        rows = fresh.scalars(select(Artwork).order_by(Artwork.id)).all()
        assert [(row.external_id, row.is_primary) for row in rows] == [("1", False), ("2", True)]
        assert (root / rows[0].local_path).is_file()
        stored = fresh.get(CatalogTitle, title.id)
        assert primary_cover_artwork(stored).id == second.id
        assert [
            (link.external_id, link.lifecycle_state, link.is_primary)
            for link in stored.external_links
        ] == links_before

    # Re-caching an already stored identity (unchanged URL and files) makes it
    # the single primary again instead of returning a demoted row untouched.
    again = cache_cover(session, catalog_title_id=title.id, provider="anilist", external_id="1",
                        remote_url="https://img/1", root=root, client=client(b"unused", "text/html"))
    session.commit()
    assert again.id == first.id
    with Session(engine) as fresh:
        assert [
            (row.external_id, row.is_primary)
            for row in fresh.scalars(select(Artwork).order_by(Artwork.id))
        ] == [("1", True), ("2", False)]
    session.close(); engine.dispose()
