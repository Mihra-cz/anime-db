from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import normalize_title
from app.media_parts import media_part_total
from app.models import CatalogTitle, MetadataCandidate, Video
from app.numbering import (
    SUPPLEMENTAL_PART_TYPES,
    effective_video_numbering,
    is_nonprimary_duplicate_video,
    summarize_title_numbering,
)

from .providers.base import MetadataProvider, ProviderTitleMetadata
from .providers.base import MetadataProviderError, MetadataRateLimitError
from .service import default_metadata_search_query
from .completion import resolve_metadata_completion

LOW_SCORE_THRESHOLD = 0.55
SCORE_MAXIMUM = 1.0


EvidenceStatus = Literal["match", "partial", "conflict", "unavailable"]


@dataclass(frozen=True)
class LocalEpisodeCountEvidence:
    """Read-only local count suitable for advisory provider comparison."""

    count: int | None
    semantics: str
    physical_video_count: int
    active_video_count: int
    standard_logical_episode_count: int
    unavailable_reasons: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.count is not None


@dataclass(frozen=True)
class EpisodeCountComparison:
    provider_count: int | None
    local: LocalEpisodeCountEvidence

    @property
    def delta(self) -> int | None:
        if self.provider_count is None or self.local.count is None:
            return None
        return abs(self.local.count - self.provider_count)

    @property
    def matches(self) -> bool | None:
        delta = self.delta
        return None if delta is None else delta == 0


@dataclass(frozen=True)
class CandidateScoreComponent:
    key: str
    label: str
    status: EvidenceStatus
    points: float
    maximum_points: float
    detail: str | None = None

    @property
    def applicable(self) -> bool:
        return self.status != "unavailable"

    def as_dict(self) -> dict:
        return asdict(self) | {"applicable": self.applicable}


@dataclass(frozen=True)
class CandidateScoreBreakdown:
    components: tuple[CandidateScoreComponent, ...]
    reasons: dict
    maximum: float = SCORE_MAXIMUM

    @property
    def raw_score(self) -> float:
        return round(sum(component.points for component in self.components), 4)

    @property
    def applicable_maximum(self) -> float:
        return round(sum(
            component.maximum_points
            for component in self.components
            if component.applicable
        ), 4)

    @property
    def normalized_score(self) -> float:
        # The historical score is an additive confidence value against a fixed
        # denominator of 1.0, not a percentage of only the available evidence.
        return min(round(self.raw_score / self.maximum, 4), 1.0)

    def as_dict(self) -> dict:
        return {
            "components": [component.as_dict() for component in self.components],
            "raw_score": self.raw_score,
            "maximum": self.maximum,
            "applicable_maximum": self.applicable_maximum,
            "normalized_score": self.normalized_score,
        }


def _same(left: str | None, right: str | None) -> bool:
    return bool(left and right and normalize_title(left) == normalize_title(right))


def _external_season_numbers(value: str) -> set[int]:
    numbers = {
        int(number)
        for number in re.findall(
            r"(?:season|part|cour)\s*([1-9]\d*)\b",
            value,
            re.IGNORECASE,
        )
    }
    numbers.update(
        int(number)
        for number in re.findall(
            r"\b([1-9]\d*)(?:st|nd|rd|th)\s+season\b",
            value,
            re.IGNORECASE,
        )
    )
    return numbers


def local_episode_count_evidence(
    title: CatalogTitle,
    videos: Iterable[Video] | None = None,
) -> LocalEpisodeCountEvidence:
    """Use canonical identities where the current model can prove a count.

    A provider count is usually a standard episode count for a Season/Part,
    while a standalone Film/OVA/Special can be represented by supplementary
    ordinals or by several physical Media Parts. Ambiguous physical-only groups
    deliberately return unavailable instead of pretending every Video is an
    episode.
    """
    video_list = list(title.videos if videos is None else videos)
    summary = summarize_title_numbering(video_list, title)
    active = tuple(
        video for video in video_list if not is_nonprimary_duplicate_video(video)
    )
    physical_count = len(video_list)
    active_count = len(active)

    if title.effective_part_type not in SUPPLEMENTAL_PART_TYPES:
        unavailable_reasons = []
        if not summary.logical_episode_count:
            unavailable_reasons.append("no_standard_logical_episode")
        if summary.unnumbered_standard:
            unavailable_reasons.append("unnumbered_standard_video")
        if summary.unknown:
            unavailable_reasons.append("unknown_video_identity")
        if summary.nonstandard:
            unavailable_reasons.append("nonstandard_video_identity")
        return LocalEpisodeCountEvidence(
            None if unavailable_reasons else summary.logical_episode_count,
            "logical_standard_episodes",
            physical_count,
            active_count,
            summary.logical_episode_count,
            tuple(unavailable_reasons),
        )

    if not active:
        return LocalEpisodeCountEvidence(
            None,
            "supplementary_logical_items",
            physical_count,
            active_count,
            summary.logical_episode_count,
            ("no_active_video",),
        )

    media_total = media_part_total(active)
    if media_total is not None and media_total == active_count:
        return LocalEpisodeCountEvidence(
            1,
            "single_logical_item_from_media_parts",
            physical_count,
            active_count,
            summary.logical_episode_count,
        )

    identities: set[tuple[str, str | None, str]] = set()
    for video in active:
        state = effective_video_numbering(video, title)
        if state.supplementary_number is not None:
            identities.add((
                "supplementary",
                state.supplementary_type or title.effective_part_type,
                str(state.supplementary_number),
            ))
        elif video.season_episode_number is not None and video.season_episode_number > 0:
            identities.add((
                "stored_title_number", None, str(video.season_episode_number),
            ))
        else:
            break
    else:
        return LocalEpisodeCountEvidence(
            len(identities),
            "supplementary_logical_items",
            physical_count,
            active_count,
            summary.logical_episode_count,
        )

    if active_count == 1:
        return LocalEpisodeCountEvidence(
            1,
            "single_supplementary_item",
            physical_count,
            active_count,
            summary.logical_episode_count,
        )
    return LocalEpisodeCountEvidence(
        None,
        "supplementary_logical_items",
        physical_count,
        active_count,
        summary.logical_episode_count,
        ("ambiguous_supplementary_identity",),
    )


def compare_episode_count(
    title: CatalogTitle,
    provider_count: int | None,
    *,
    local_evidence: LocalEpisodeCountEvidence | None = None,
    videos: Iterable[Video] | None = None,
) -> EpisodeCountComparison:
    return EpisodeCountComparison(
        provider_count,
        local_evidence or local_episode_count_evidence(title, videos),
    )


def _component(
    key: str,
    label: str,
    status: EvidenceStatus,
    points: float,
    maximum_points: float,
    detail: str | None = None,
) -> CandidateScoreComponent:
    return CandidateScoreComponent(
        key, label, status, points, maximum_points, detail,
    )


def score_candidate_breakdown(
    title: CatalogTitle,
    data: ProviderTitleMetadata,
    query: str | None = None,
    *,
    local_episode_evidence: LocalEpisodeCountEvidence | None = None,
) -> CandidateScoreBreakdown:
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
    external_season_numbers = _external_season_numbers(external_names)
    season_match = (
        None
        if season_number is None or not external_season_numbers
        else season_number in external_season_numbers
    )
    local_year_match = re.search(r"\b(19\d{2}|20\d{2})\b", title.local_title)
    year_match = None if not local_year_match or data.release_year is None else int(local_year_match.group()) == data.release_year
    episode_comparison = compare_episode_count(
        title,
        data.episode_count,
        local_evidence=local_episode_evidence,
    )
    episode_delta = episode_comparison.delta
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
        "local_episode_count": episode_comparison.local.count,
        "local_episode_count_semantics": episode_comparison.local.semantics,
        "local_physical_video_count": episode_comparison.local.physical_video_count,
        "episode_count_evidence_available": episode_comparison.local.available,
        "format_match": format_match,
    }
    provider_has_title = any((
        data.title_romaji, data.title_english, data.title_native, *data.synonyms,
    ))
    if title_exact:
        title_status: EvidenceStatus = "match"
        title_points = 0.58
        title_detail = "exact provider title variant"
    elif alias_match:
        title_status = "partial"
        title_points = 0.38
        title_detail = "provider synonym"
    elif provider_has_title:
        title_status = "conflict"
        title_points = 0.0
        title_detail = "no exact local title variant"
    else:
        title_status = "unavailable"
        title_points = 0.0
        title_detail = "provider supplied no title variant"

    components = [
        _component(
            "title_identity", "Title exact / alternate", title_status,
            title_points, 0.58, title_detail,
        ),
        _component(
            "romaji_exact_bonus", "Romaji exact bonus",
            "match" if romaji_match else "conflict" if data.title_romaji else "unavailable",
            0.08 if romaji_match else 0.0, 0.08,
        ),
        _component(
            "english_exact_bonus", "English exact bonus",
            "match" if english_match else "conflict" if data.title_english else "unavailable",
            0.10 if english_match else 0.0, 0.10,
        ),
        _component(
            "season_name_evidence", "Season token in provider title",
            "match" if season_match is True else "conflict" if season_match is False else "unavailable",
            0.08 if season_match is True else 0.0, 0.08,
        ),
        _component(
            "year_evidence", "Four-digit local year",
            "match" if year_match is True else "conflict" if year_match is False else "unavailable",
            0.06 if year_match is True else 0.0, 0.06,
        ),
        _component(
            "episode_count_evidence", "Episode count",
            (
                "match" if episode_delta == 0
                else "partial" if episode_delta is not None and episode_delta <= 3
                else "conflict" if episode_delta is not None
                else "unavailable"
            ),
            (
                0.06 if episode_delta == 0
                else 0.04 if episode_delta is not None and episode_delta <= 1
                else 0.02 if episode_delta is not None and episode_delta <= 3
                else 0.0
            ),
            0.06,
            (
                f"local={episode_comparison.local.count}, "
                f"provider={data.episode_count}, delta={episode_delta}, "
                f"semantics={episode_comparison.local.semantics}"
            ),
        ),
        _component(
            "format_evidence", "Format",
            "match" if format_match is True else "conflict" if format_match is False else "unavailable",
            0.04 if format_match is True else 0.0, 0.04,
        ),
    ]
    breakdown = CandidateScoreBreakdown(tuple(components), reasons)
    reasons["score_breakdown"] = breakdown.as_dict()
    return breakdown


def score_candidate(
    title: CatalogTitle,
    data: ProviderTitleMetadata,
    query: str | None = None,
    *,
    local_episode_evidence: LocalEpisodeCountEvidence | None = None,
) -> tuple[float, dict]:
    breakdown = score_candidate_breakdown(
        title,
        data,
        query,
        local_episode_evidence=local_episode_evidence,
    )
    return breakdown.normalized_score, breakdown.reasons


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
    local_episode_evidence = local_episode_count_evidence(title)
    for data in results[:max(0, min(limit, 10))]:
        identity = (data.provider.strip().casefold(), str(data.external_id))
        if identity in seen:
            continue
        seen.add(identity)
        score, reasons = score_candidate(
            title,
            data,
            query,
            local_episode_evidence=local_episode_evidence,
        )
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
            if resolve_metadata_completion(title, title.videos).resolved:
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
