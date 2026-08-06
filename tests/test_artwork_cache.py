from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.metadata import artwork as artwork_module
from app.metadata.artwork import ArtworkCacheError, cache_cover
from app.models import Artwork, CatalogTitle


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
