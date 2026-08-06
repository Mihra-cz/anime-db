from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import normalize_title
from app.models import CatalogTitle, MetadataCandidate

from .providers.base import MetadataProvider, ProviderTitleMetadata
from .providers.base import MetadataProviderError, MetadataRateLimitError
from .service import default_metadata_search_query

LOW_SCORE_THRESHOLD = 0.55


def _same(left: str | None, right: str | None) -> bool:
    return bool(left and right and normalize_title(left) == normalize_title(right))


def score_candidate(title: CatalogTitle, data: ProviderTitleMetadata, query: str | None = None) -> tuple[float, dict]:
    local_names = {title.local_title, default_metadata_search_query(title)}
    if query:
        local_names.add(query)
    normalized_local = {normalize_title(value) for value in local_names if value}
    romaji_match = normalize_title(data.title_romaji or "") in normalized_local
    english_match = normalize_title(data.title_english or "") in normalized_local
    native_match = normalize_title(data.title_native or "") in normalized_local
    alias_match = any(normalize_title(alias) in normalized_local for alias in data.synonyms)
    title_exact = romaji_match or english_match or native_match

    season_number = title.effective_season_number
    external_names = " ".join(filter(None, [data.title_romaji, data.title_english, *data.synonyms]))
    season_match = None if season_number is None else bool(re.search(
        rf"(?:season|part|cour)\s*{season_number}\b|\b{season_number}(?:st|nd|rd|th)\s+season\b",
        external_names, re.IGNORECASE,
    ))
    local_year_match = re.search(r"\b(19\d{2}|20\d{2})\b", title.local_title)
    year_match = None if not local_year_match or data.release_year is None else int(local_year_match.group()) == data.release_year
    local_episodes = len([video for video in title.videos if video.file_type == "episode"])
    episode_delta = None if not local_episodes or data.episode_count is None else abs(local_episodes - data.episode_count)
    expected_formats = {
        "film": {"MOVIE"}, "ova": {"OVA"}, "special": {"SPECIAL"},
        "title": {"TV", "TV_SHORT", "ONA"}, "season": {"TV", "TV_SHORT", "ONA"},
        "part": {"TV", "TV_SHORT", "ONA"}, "cour": {"TV", "TV_SHORT", "ONA"},
    }.get(title.effective_part_type)
    format_match = None if not expected_formats or not data.format else data.format.upper() in expected_formats
    reasons = {
        "title_exact": title_exact,
        "romaji_title_match": romaji_match,
        "english_title_match": english_match,
        "native_title_match": native_match,
        "alias_match": alias_match,
        "season_match": season_match,
        "year_match": year_match,
        "episode_count_delta": episode_delta,
        "format_match": format_match,
    }
    score = 0.0
    if title_exact:
        score += 0.58
    elif alias_match:
        score += 0.38
    if romaji_match:
        score += 0.08
    if english_match:
        score += 0.10
    if season_match is True:
        score += 0.08
    if year_match is True:
        score += 0.06
    if episode_delta is not None:
        score += 0.06 if episode_delta == 0 else 0.04 if episode_delta <= 1 else 0.02 if episode_delta <= 3 else 0
    if format_match is True:
        score += 0.04
    return min(round(score, 4), 1.0), reasons


def candidate_title(data: ProviderTitleMetadata) -> str:
    return data.title_romaji or data.title_english or data.title_native or f"{data.provider} {data.external_id}"


def _raw_payload(data: ProviderTitleMetadata) -> str:
    return json.dumps(asdict(data), ensure_ascii=False, separators=(",", ":"))


def store_candidates(
    session: Session, title: CatalogTitle, results: list[ProviderTitleMetadata],
    *, query: str | None = None, limit: int = 10, now: datetime | None = None,
) -> list[MetadataCandidate]:
    timestamp = now or datetime.now(timezone.utc)
    stored: list[MetadataCandidate] = []
    seen: set[tuple[str, str]] = set()
    for data in results[:max(0, min(limit, 10))]:
        identity = (data.provider.strip().casefold(), str(data.external_id))
        if identity in seen:
            continue
        seen.add(identity)
        score, reasons = score_candidate(title, data, query)
        candidate = session.scalar(select(MetadataCandidate).where(
            MetadataCandidate.catalog_title_id == title.id,
            MetadataCandidate.provider == identity[0],
            MetadataCandidate.external_id == identity[1],
        ))
        if candidate is None:
            candidate = MetadataCandidate(
                catalog_title_id=title.id, provider=identity[0], external_id=identity[1],
                candidate_title=candidate_title(data), created_at=timestamp,
            )
            session.add(candidate)
        candidate.candidate_title = candidate_title(data)
        candidate.title_romaji = data.title_romaji
        candidate.title_english = data.title_english
        candidate.title_native = data.title_native
        candidate.candidate_year = data.release_year
        candidate.candidate_format = data.format
        candidate.candidate_episode_count = data.episode_count
        candidate.cover_image_url = data.cover_image_url
        candidate.site_url = data.site_url
        candidate.match_score = score
        candidate.match_reasons_json = json.dumps(reasons, ensure_ascii=False)
        candidate.raw_payload_json = _raw_payload(data)
        candidate.updated_at = timestamp
        stored.append(candidate)
    if stored and title.metadata_status == "unlinked":
        title.metadata_status = "candidates_available"
    elif not stored and title.metadata_status == "unlinked":
        title.metadata_status = "unavailable"
    session.flush()
    return stored


def search_and_store_candidates(
    session: Session, title: CatalogTitle, query: str, provider: MetadataProvider,
    *, limit: int = 10,
) -> list[MetadataCandidate]:
    results = provider.search_titles(query)
    return store_candidates(session, title, results, query=query, limit=limit)


def set_candidate_rejected(session: Session, title_id: int, candidate_id: int, rejected: bool) -> MetadataCandidate:
    candidate = session.scalar(select(MetadataCandidate).where(
        MetadataCandidate.id == candidate_id,
        MetadataCandidate.catalog_title_id == title_id,
    ))
    if candidate is None:
        raise ValueError("Kandidát pro tento titul nebyl nalezen.")
    candidate.rejected_at = datetime.now(timezone.utc) if rejected else None
    return candidate


def decode_match_reasons(candidate: MetadataCandidate) -> dict:
    try:
        value = json.loads(candidate.match_reasons_json or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


@dataclass
class BatchSearchResult:
    processed: int = 0
    candidates: int = 0
    empty: int = 0
    errors: int = 0
    skipped: int = 0
    warnings: int = 0


def batch_search_candidates(session_factory, provider: MetadataProvider, *, limit: int = 10,
                            candidate_limit: int = 10, throttle=None) -> BatchSearchResult:
    result = BatchSearchResult()
    with session_factory() as session:
        title_ids = list(session.scalars(select(CatalogTitle.id).where(
            CatalogTitle.metadata_status == "unlinked"
        ).order_by(CatalogTitle.id)).all())
    attempted = 0
    for title_id in title_ids:
        with session_factory() as session:
            title = session.get(CatalogTitle, title_id)
            if title is None or title.metadata_status != "unlinked":
                result.skipped += 1
                continue
            if title.metadata_locked or title.collection and title.collection.hierarchy_status == "conflict":
                result.skipped += 1
                continue
            if attempted >= max(1, limit):
                break
            attempted += 1
            if title.collection and title.collection.hierarchy_status == "review_required":
                result.warnings += 1
            try:
                found = search_and_store_candidates(
                    session, title, default_metadata_search_query(title), provider,
                    limit=candidate_limit,
                )
                session.commit()
                result.processed += 1
                result.candidates += len(found)
                result.empty += not found
            except MetadataRateLimitError:
                session.rollback()
                result.errors += 1
                break
            except (ValueError, MetadataProviderError):
                session.rollback()
                result.processed += 1
                result.errors += 1
        if throttle and attempted < limit:
            throttle()
    return result
