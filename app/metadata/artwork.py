from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Artwork
from .providers.base import metadata_http_timeout


ALLOWED_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_-]+$")


class ArtworkCacheError(RuntimeError):
    pass


def resolve_local_path(root: Path, local_path: str) -> Path:
    relative = PurePosixPath(local_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ArtworkCacheError("Neplatná cesta lokálního obalu.")
    resolved_root = root.resolve()
    resolved = (resolved_root / Path(*relative.parts)).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ArtworkCacheError("Cesta obalu opouští adresář cache.")
    return resolved


def _identity_path(provider: str, external_id: str) -> PurePosixPath:
    normalized_provider = provider.strip().casefold()
    normalized_id = str(external_id).strip()
    if not _SAFE_COMPONENT.fullmatch(normalized_provider) or not _SAFE_COMPONENT.fullmatch(normalized_id):
        raise ArtworkCacheError("Neplatná identita provideru nebo obalu.")
    return PurePosixPath(normalized_provider, normalized_id)


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ArtworkCacheError("URL obalu musí být bezpečná HTTP nebo HTTPS adresa.")


def cache_cover(
    session: Session, *, catalog_title_id: int, provider: str, external_id: str,
    remote_url: str, root: Path, max_bytes: int = 10_485_760,
    thumbnail_width: int = 400, timeout_seconds: float = 15,
    client: httpx.Client | None = None, force: bool = False,
) -> Artwork:
    """Download first, atomically publish both files, then update the database row."""
    _validate_url(remote_url)
    identity = _identity_path(provider, external_id)
    existing = session.scalar(select(Artwork).where(
        Artwork.catalog_title_id == catalog_title_id,
        Artwork.provider == provider.strip().casefold(),
        Artwork.external_id == str(external_id),
        Artwork.artwork_type == "cover",
    ))
    if existing and not force and existing.remote_url == remote_url:
        original = resolve_local_path(root, existing.local_path)
        thumbnail = resolve_local_path(root, existing.thumbnail_path) if existing.thumbnail_path else None
        if original.is_file() and thumbnail and thumbnail.is_file():
            return existing

    destination = resolve_local_path(root, identity.as_posix())
    destination.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    request_timeout = metadata_http_timeout(timeout_seconds)
    http = client or httpx.Client(follow_redirects=True, timeout=request_timeout)
    original_temp: Path | None = None
    thumbnail_temp: Path | None = None
    try:
        with http.stream("GET", remote_url, timeout=request_timeout) as response:
            response.raise_for_status()
            mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
            extension = ALLOWED_MIME_TYPES.get(mime_type)
            if extension is None:
                raise ArtworkCacheError("Server nevrátil podporovaný typ obrázku.")
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise ArtworkCacheError("Obal překračuje povolenou maximální velikost.")
            with tempfile.NamedTemporaryFile(prefix=".cover-original-", suffix=extension, dir=destination, delete=False) as output:
                original_temp = Path(output.name)
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ArtworkCacheError("Obal překračuje povolenou maximální velikost.")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if size <= 0:
            raise ArtworkCacheError("Server vrátil prázdný obrázek.")

        try:
            from PIL import Image
            with Image.open(original_temp) as image:
                image.verify()
            with Image.open(original_temp) as image:
                width, height = image.size
                image.thumbnail((thumbnail_width, max(1, int(height * thumbnail_width / max(width, 1)))))
                with tempfile.NamedTemporaryFile(prefix=".cover-thumb-", suffix=".webp", dir=destination, delete=False) as thumb:
                    thumbnail_temp = Path(thumb.name)
                image.save(thumbnail_temp, format="WEBP", quality=85, method=6)
        except ArtworkCacheError:
            raise
        except Exception as exc:
            raise ArtworkCacheError("Stažený soubor není platný podporovaný obrázek.") from exc

        final_original = destination / f"cover-original{extension}"
        final_thumbnail = destination / "cover-thumb.webp"
        os.replace(original_temp, final_original)
        original_temp = None
        os.replace(thumbnail_temp, final_thumbnail)
        thumbnail_temp = None
        old_original = resolve_local_path(root, existing.local_path) if existing else None
        if old_original and old_original != final_original and old_original.is_file():
            old_original.unlink()

        timestamp = datetime.now(timezone.utc)
        artwork = existing or Artwork(
            catalog_title_id=catalog_title_id, provider=provider.strip().casefold(),
            external_id=str(external_id), artwork_type="cover",
        )
        if existing is None:
            session.add(artwork)
        artwork.remote_url = remote_url
        artwork.local_path = (identity / final_original.name).as_posix()
        artwork.thumbnail_path = (identity / final_thumbnail.name).as_posix()
        artwork.mime_type = mime_type
        artwork.width = width
        artwork.height = height
        artwork.file_size = size
        artwork.is_primary = True
        artwork.fetched_at = timestamp
        artwork.updated_at = timestamp
        session.flush()
        return artwork
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPError) as exc:
        raise ArtworkCacheError("Obal se nepodařilo stáhnout.") from exc
    finally:
        if owns_client:
            http.close()
        for temporary in (original_temp, thumbnail_temp):
            if temporary and temporary.exists():
                temporary.unlink()
