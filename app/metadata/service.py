from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CatalogTitle, ExternalTitleLink, TitleMetadata

from .providers.base import MetadataProvider, ProviderTitleMetadata


class MetadataConflictError(RuntimeError):
    pass


class MetadataLockedError(RuntimeError):
    pass


class _PlainTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1


def sanitize_description(value: str | None) -> str | None:
    if not value:
        return None
    parser = _PlainTextParser()
    parser.feed(value)
    parser.close()
    lines = (" ".join(line.split()) for line in unescape("".join(parser.parts)).splitlines())
    cleaned = "\n".join(line for line in lines if line)
    return cleaned or None


def preferred_display_title(title: CatalogTitle, data: ProviderTitleMetadata | None) -> str:
    return (
        title.manual_display_title
        or (data.title_english if data else None)
        or (data.title_romaji if data else None)
        or (data.title_native if data else None)
        or title.local_title
    )


def _write_metadata(
    session: Session, title: CatalogTitle, data: ProviderTitleMetadata, now: datetime
) -> TitleMetadata:
    metadata = session.get(TitleMetadata, title.id)
    is_same_source = bool(
        metadata
        and metadata.metadata_provider == data.provider
        and metadata.metadata_external_id == data.external_id
    )
    if metadata is None:
        metadata = TitleMetadata(catalog_title_id=title.id, display_title=title.local_title)
        session.add(metadata)
    metadata.display_title = preferred_display_title(title, data)
    metadata.title_romaji = data.title_romaji
    metadata.title_english = data.title_english
    metadata.title_native = data.title_native
    metadata.description = sanitize_description(data.description)
    metadata.release_year = data.release_year
    metadata.season = data.season
    metadata.format = data.format
    metadata.status = data.status
    metadata.episode_count = data.episode_count
    metadata.episode_duration_minutes = data.episode_duration_minutes
    metadata.genres_json = json.dumps(data.genres, ensure_ascii=False)
    metadata.tags_json = json.dumps(data.tags, ensure_ascii=False)
    metadata.synonyms_json = json.dumps(data.synonyms, ensure_ascii=False)
    metadata.country_of_origin = data.country_of_origin
    metadata.is_adult = data.is_adult
    metadata.metadata_provider = data.provider
    metadata.metadata_external_id = data.external_id
    metadata.cover_image_url = data.cover_image_url
    metadata.metadata_fetched_at = metadata.metadata_fetched_at if is_same_source else now
    metadata.metadata_updated_at = now
    return metadata


def confirm_anilist_candidate(
    session: Session,
    title: CatalogTitle,
    external_id: str,
    provider: MetadataProvider,
    *,
    confirm_conflict: bool = False,
    confirm_locked: bool = False,
    now: datetime | None = None,
) -> ExternalTitleLink:
    data = provider.fetch_title(external_id)
    if data.provider != "anilist" or data.external_id != str(int(external_id)):
        raise ValueError("AniList vrátil neočekávanou identitu titulu.")
    current_primary = session.scalar(select(ExternalTitleLink).where(
        ExternalTitleLink.catalog_title_id == title.id,
        ExternalTitleLink.is_primary.is_(True),
    ))
    if title.metadata_locked and current_primary and not confirm_locked:
        raise MetadataLockedError(
            "Metadata jsou zamknutá; jejich opětovné potvrzení nebo změnu vazby "
            "je nutné výslovně potvrdit."
        )
    conflicting = session.scalar(select(ExternalTitleLink).where(
        ExternalTitleLink.provider == "anilist",
        ExternalTitleLink.external_id == data.external_id,
        ExternalTitleLink.is_primary.is_(True),
        ExternalTitleLink.catalog_title_id != title.id,
    ))
    if conflicting and not confirm_conflict:
        raise MetadataConflictError(
            "Toto AniList ID je už primární vazbou jiného lokálního titulu. "
            "Potvrďte vědomé použití stejného externího ID."
        )
    timestamp = now or datetime.now(timezone.utc)
    for link in session.scalars(select(ExternalTitleLink).where(
        ExternalTitleLink.catalog_title_id == title.id,
        ExternalTitleLink.is_primary.is_(True),
    )):
        link.is_primary = False
    link = session.scalar(select(ExternalTitleLink).where(
        ExternalTitleLink.catalog_title_id == title.id,
        ExternalTitleLink.provider == "anilist",
        ExternalTitleLink.external_id == data.external_id,
    ))
    if link is None:
        link = ExternalTitleLink(
            catalog_title_id=title.id, provider="anilist", external_id=data.external_id,
            match_method="manual_search",
        )
        session.add(link)
    link.external_url = data.site_url
    link.match_method = "manual_search"
    link.is_primary = True
    link.is_manual = True
    link.verified_at = timestamp
    title.preferred_metadata_provider = "anilist"
    title.preferred_external_id = data.external_id
    title.metadata_status = "linked_manual"
    _write_metadata(session, title, data, timestamp)
    session.flush()
    return link


def refresh_title_metadata(
    session: Session, title: CatalogTitle, provider: MetadataProvider,
    *, now: datetime | None = None,
) -> TitleMetadata:
    if title.metadata_locked:
        raise MetadataLockedError("Metadata jsou zamknutá. Před aktualizací je odemkněte.")
    link = session.scalar(select(ExternalTitleLink).where(
        ExternalTitleLink.catalog_title_id == title.id,
        ExternalTitleLink.is_primary.is_(True),
    ))
    if link is None or link.provider != "anilist":
        raise ValueError("Titul nemá primární AniList vazbu.")
    data = provider.fetch_title(link.external_id)
    if data.external_id != link.external_id:
        raise ValueError("AniList vrátil neočekávanou identitu titulu.")
    timestamp = now or datetime.now(timezone.utc)
    link.external_url = data.site_url
    return _write_metadata(session, title, data, timestamp)


def unlink_title_metadata(session: Session, title: CatalogTitle) -> None:
    for link in session.scalars(select(ExternalTitleLink).where(
        ExternalTitleLink.catalog_title_id == title.id,
    )):
        link.is_primary = False
    if metadata := session.get(TitleMetadata, title.id):
        session.delete(metadata)
    title.preferred_metadata_provider = None
    title.preferred_external_id = None
    title.metadata_status = "unlinked"


def set_manual_display_title(session: Session, title: CatalogTitle, value: str) -> None:
    normalized = value.strip()
    if len(normalized) > 200:
        raise ValueError("Ruční zobrazovaný název může mít nejvýše 200 znaků.")
    title.manual_display_title = normalized or None
    metadata = session.get(TitleMetadata, title.id)
    if metadata:
        data = ProviderTitleMetadata(
            provider=metadata.metadata_provider or "anilist",
            external_id=metadata.metadata_external_id or "",
            title_romaji=metadata.title_romaji, title_english=metadata.title_english,
            title_native=metadata.title_native,
        )
        metadata.display_title = preferred_display_title(title, data)
