from __future__ import annotations

from typing import Any

import httpx

from .base import MetadataProviderError, MetadataRateLimitError, ProviderTitleMetadata

ANILIST_ENDPOINT = "https://graphql.anilist.co"
MEDIA_FIELDS = """
id
title { romaji english native }
synonyms
seasonYear
season
format
status
episodes
description(asHtml: false)
coverImage { medium large }
siteUrl
"""
SEARCH_QUERY = """
query SearchAnime($search: String!, $perPage: Int!) {
  Page(page: 1, perPage: $perPage) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      %s
    }
  }
}
""" % MEDIA_FIELDS
TITLE_QUERY = """
query AnimeById($id: Int!) {
  Media(id: $id, type: ANIME) {
    %s
  }
}
""" % MEDIA_FIELDS


class AniListProvider:
    name = "anilist"

    def __init__(self, timeout_seconds: float = 15, client: httpx.Client | None = None):
        self.timeout_seconds = timeout_seconds
        self.client = client

    def _request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            if self.client is not None:
                response = self.client.post(
                    ANILIST_ENDPOINT, json={"query": query, "variables": variables},
                    timeout=self.timeout_seconds,
                )
            else:
                response = httpx.post(
                    ANILIST_ENDPOINT, json={"query": query, "variables": variables},
                    timeout=self.timeout_seconds,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MetadataProviderError("AniList není momentálně dostupný.") from exc
        if response.status_code == 429:
            raise MetadataRateLimitError("AniList dočasně omezil počet požadavků.")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MetadataProviderError(f"AniList vrátil HTTP chybu {response.status_code}.") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise MetadataProviderError("AniList vrátil neplatnou odpověď.") from exc
        if payload.get("errors"):
            raise MetadataProviderError("AniList GraphQL požadavek skončil chybou.")
        return payload.get("data") or {}

    @staticmethod
    def _normalize(media: dict[str, Any]) -> ProviderTitleMetadata:
        title = media.get("title") or {}
        cover = media.get("coverImage") or {}
        return ProviderTitleMetadata(
            provider="anilist", external_id=str(media["id"]),
            title_romaji=title.get("romaji"), title_english=title.get("english"),
            title_native=title.get("native"), synonyms=list(media.get("synonyms") or []),
            release_year=media.get("seasonYear"), season=media.get("season"),
            format=media.get("format"), status=media.get("status"),
            episode_count=media.get("episodes"), description=media.get("description"),
            cover_image_url=cover.get("medium") or cover.get("large"),
            site_url=media.get("siteUrl"),
        )

    def search_titles(self, query: str) -> list[ProviderTitleMetadata]:
        normalized = query.strip()
        if not normalized:
            raise ValueError("Vyhledávací dotaz nesmí být prázdný.")
        if len(normalized) > 200:
            raise ValueError("Vyhledávací dotaz může mít nejvýše 200 znaků.")
        data = self._request(SEARCH_QUERY, {"search": normalized, "perPage": 10})
        return [self._normalize(item) for item in ((data.get("Page") or {}).get("media") or [])]

    def fetch_title(self, external_id: str) -> ProviderTitleMetadata:
        try:
            numeric_id = int(external_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("AniList ID musí být číslo.") from exc
        data = self._request(TITLE_QUERY, {"id": numeric_id})
        media = data.get("Media")
        if not media:
            raise MetadataProviderError("Titul na AniListu nebyl nalezen.")
        return self._normalize(media)

    def fetch_relations(self, external_id: str):
        raise NotImplementedError("V této iteraci nejsou relace implementované.")

    def fetch_artwork(self, external_id: str):
        raise NotImplementedError("V této iteraci není stahování obrázků implementované.")
