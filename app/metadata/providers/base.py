from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx


def metadata_http_timeout(read_seconds: float = 15) -> httpx.Timeout:
    read = max(0.1, float(read_seconds))
    return httpx.Timeout(
        connect=min(5.0, read), read=read, write=min(10.0, read), pool=min(5.0, read)
    )


class MetadataProviderError(RuntimeError):
    """Bezpečně zobrazitelná chyba externího provideru."""


class MetadataRateLimitError(MetadataProviderError):
    pass


@dataclass(frozen=True)
class ProviderTitleMetadata:
    provider: str
    external_id: str
    title_romaji: str | None = None
    title_english: str | None = None
    title_native: str | None = None
    synonyms: list[str] = field(default_factory=list)
    release_year: int | None = None
    season: str | None = None
    format: str | None = None
    status: str | None = None
    episode_count: int | None = None
    episode_duration_minutes: int | None = None
    description: str | None = None
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    country_of_origin: str | None = None
    is_adult: bool | None = None
    cover_image_url: str | None = None
    site_url: str | None = None


@dataclass(frozen=True)
class ProviderRelation:
    provider: str
    external_id: str
    relation_type: str


@dataclass(frozen=True)
class ProviderArtwork:
    provider: str
    external_id: str
    artwork_type: str
    remote_url: str


class MetadataProvider(Protocol):
    name: str

    def search_titles(self, query: str) -> list[ProviderTitleMetadata]: ...
    def fetch_title(self, external_id: str) -> ProviderTitleMetadata: ...
    def fetch_relations(self, external_id: str) -> list[ProviderRelation]: ...
    def fetch_artwork(self, external_id: str) -> list[ProviderArtwork]: ...
