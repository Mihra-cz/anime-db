from __future__ import annotations

from typing import Any

import httpx

from .base import MetadataProviderError, MetadataRateLimitError, ProviderTitleMetadata, metadata_http_timeout

ANILIST_ENDPOINT = "https://graphql.anilist.co"
ANILIST_COMMUNICATION_ERROR = "Nepodařilo se komunikovat s AniList API."
ANILIST_OUTAGE_ERROR = (
    "AniList API je momentálně dočasně nedostupné kvůli problémům na straně "
    "AniListu. Zkuste akci později."
)
ANILIST_RATE_LIMIT_ERROR = (
    "Byl dosažen limit požadavků AniList API. Zkuste akci znovu později."
)
ANILIST_TEMPORARY_DISABLED_PREFIX = (
    "the anilist api has been temporarily disabled due to severe stability issues"
)
MEDIA_FIELDS = """
id
title { romaji english native }
synonyms
seasonYear
season
format
status
episodes
duration
description(asHtml: false)
genres
tags { name rank isMediaSpoiler }
countryOfOrigin
isAdult
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
        self.timeout = metadata_http_timeout(timeout_seconds)
        self.client = client

    @staticmethod
    def _graphql_error_messages(payload: object) -> tuple[str, ...]:
        if not isinstance(payload, dict) or not isinstance(payload.get("errors"), list):
            return ()
        return tuple(
            message.strip()
            for error in payload["errors"]
            if isinstance(error, dict)
            and isinstance((message := error.get("message")), str)
            and message.strip()
        )

    @classmethod
    def _is_temporary_outage(cls, payload: object) -> bool:
        return any(
            " ".join(message.split()).casefold().startswith(
                ANILIST_TEMPORARY_DISABLED_PREFIX
            )
            for message in cls._graphql_error_messages(payload)
        )

    @staticmethod
    def _rate_limit_message(response: httpx.Response) -> str:
        raw_retry_after = response.headers.get("Retry-After")
        try:
            retry_after = int(raw_retry_after.strip()) if raw_retry_after else None
        except ValueError:
            retry_after = None
        if retry_after is None or retry_after < 1:
            return ANILIST_RATE_LIMIT_ERROR
        return (
            "Byl dosažen limit požadavků AniList API. Zkuste akci znovu "
            f"přibližně za {retry_after} sekund."
        )

    def _request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            if self.client is not None:
                response = self.client.post(
                    ANILIST_ENDPOINT, json={"query": query, "variables": variables},
                    timeout=self.timeout,
                )
            else:
                response = httpx.post(
                    ANILIST_ENDPOINT, json={"query": query, "variables": variables},
                    timeout=self.timeout,
                )
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MetadataProviderError(ANILIST_COMMUNICATION_ERROR) from exc

        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code == 429:
            raise MetadataRateLimitError(self._rate_limit_message(response))
        if response.status_code == 403 and self._is_temporary_outage(payload):
            raise MetadataProviderError(ANILIST_OUTAGE_ERROR)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MetadataProviderError(
                f"{ANILIST_COMMUNICATION_ERROR} HTTP stav {response.status_code}."
            ) from exc
        if not isinstance(payload, dict):
            raise MetadataProviderError(
                f"{ANILIST_COMMUNICATION_ERROR} AniList vrátil neplatnou odpověď."
            )
        if payload.get("errors"):
            raise MetadataProviderError(
                f"{ANILIST_COMMUNICATION_ERROR} GraphQL odpověď obsahovala chybu."
            )
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
            episode_duration_minutes=media.get("duration"),
            genres=list(media.get("genres") or []),
            tags=[tag.get("name") for tag in (media.get("tags") or []) if tag.get("name")],
            country_of_origin=media.get("countryOfOrigin"), is_adult=media.get("isAdult"),
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
        if numeric_id <= 0 or str(numeric_id) != str(external_id).strip():
            raise ValueError("AniList ID musí být kladné celé číslo.")
        data = self._request(TITLE_QUERY, {"id": numeric_id})
        media = data.get("Media")
        if not media:
            raise MetadataProviderError("Titul na AniListu nebyl nalezen.")
        return self._normalize(media)

    def fetch_relations(self, external_id: str):
        raise NotImplementedError("V této iteraci nejsou relace implementované.")

    def fetch_artwork(self, external_id: str):
        raise NotImplementedError("V této iteraci není stahování obrázků implementované.")
