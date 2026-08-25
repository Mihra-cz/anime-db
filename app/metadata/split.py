from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.catalog import detect_episode_number
from app.hierarchy_evaluation import catalog_title_hierarchy_is_verified
from app.hierarchy_review import create_title_from_videos, refresh_collection_state
from app.hierarchy_types import PART_TYPE_LABELS, PART_TYPES
from app.manual_split import (
    ManualSplitDecisionKind,
    evaluate_persisted_manual_split,
)
from app.models import (
    CatalogCollection, CatalogTitle, ExternalTitleLink, TitleMetadata, Video,
)
from app.numbering import (
    effective_video_numbering,
    recalculate_title_numbering,
)


class MetadataSplitStatus(StrEnum):
    RECOMMENDED = "recommended"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class MetadataSplitEvaluation:
    """Read-only result of matching one confirmed metadata title to local videos."""

    status: MetadataSplitStatus
    title: CatalogTitle
    metadata: TitleMetadata
    primary_link: ExternalTitleLink
    matching_videos: tuple[Video, ...] = ()
    remaining_videos: tuple[Video, ...] = ()
    reason: str | None = None

    @property
    def is_recommendation(self) -> bool:
        return self.status == MetadataSplitStatus.RECOMMENDED

    @property
    def is_ambiguous(self) -> bool:
        return self.status == MetadataSplitStatus.AMBIGUOUS

    @property
    def metadata_title(self) -> str:
        return self.metadata.display_title

    @property
    def episode_count(self) -> int:
        assert self.metadata.episode_count is not None
        return self.metadata.episode_count

    @property
    def part_type(self) -> str:
        return self.title.effective_part_type

    @property
    def part_type_label(self) -> str:
        return PART_TYPE_LABELS.get(
            self.part_type, self.part_type.replace("_", " ").title()
        )

    @property
    def season_context(self) -> str:
        label = self.title.effective_season_label
        if label:
            return label
        number = self.title.effective_season_number
        return f"S{number}" if number is not None else "úroveň anime"

    @property
    def proposed_local_title(self) -> str:
        return _metadata_split_local_title(self.title, self.metadata)


@dataclass(frozen=True)
class MetadataSplitResult:
    source_title: CatalogTitle
    new_title: CatalogTitle
    moved_videos: tuple[Video, ...]
    remaining_videos: tuple[Video, ...]


def _confirmed_metadata(
    title: CatalogTitle,
) -> tuple[TitleMetadata, ExternalTitleLink] | None:
    metadata = title.metadata_record
    primary = next((link for link in title.external_links if link.is_primary), None)
    if (
        metadata is None
        or primary is None
        or title.metadata_status != "linked_manual"
        or not primary.is_manual
        or primary.verified_at is None
        or metadata.metadata_provider != primary.provider
        or metadata.metadata_external_id != primary.external_id
        or metadata.episode_count is None
        or metadata.episode_count <= 0
    ):
        return None
    return metadata, primary


def _safe_metadata_position(
    video: Video, title: CatalogTitle,
) -> tuple[str, int] | None:
    state = effective_video_numbering(video, title)
    if state.is_supplementary and state.supplementary_number is not None:
        return (
            f"supplementary:{state.supplementary_type or title.effective_part_type}",
            state.supplementary_number,
        )
    if state.is_standard and state.numbering_input is not None:
        return "standard", state.numbering_input

    # A verified OVA/Special/Bonus/Film title can legitimately contain generic
    # ``01``, ``02`` filenames.  The concrete title authority supplies the
    # missing subtype, while the parser still supplies an explicit ordinal.
    detection = detect_episode_number(video.filename)
    if (
        state.is_supplementary
        and title.effective_part_type in PART_TYPES
        and catalog_title_hierarchy_is_verified(title)
        and detection.is_standard
        and detection.number is not None
    ):
        return f"title:{title.effective_part_type}", detection.number
    return None


def _ambiguous(
    title: CatalogTitle,
    metadata: TitleMetadata,
    primary: ExternalTitleLink,
    reason: str,
) -> MetadataSplitEvaluation:
    return MetadataSplitEvaluation(
        MetadataSplitStatus.AMBIGUOUS,
        title,
        metadata,
        primary,
        reason=reason,
    )


def evaluate_metadata_split(
    title: CatalogTitle,
) -> MetadataSplitEvaluation | None:
    """Recommend a split only for an exact 1..N numbering match.

    Merely having a filename pattern or a provider episode count is never
    enough.  The complete local logical group must form one unambiguous
    numbering sequence and the confirmed metadata count must cover a strict
    prefix of that sequence.
    """
    confirmed = _confirmed_metadata(title)
    if confirmed is None or title.effective_part_type not in PART_TYPES:
        return None
    metadata, primary = confirmed
    logical_videos = tuple(
        sorted(
            (
                video for video in title.videos
                if (
                    video.duplicate_of_video_id is None
                    and not video.duplicate_primary_missing
                )
            ),
            key=lambda video: (video.relative_path.casefold(), video.id or 0),
        )
    )
    episode_count = metadata.episode_count
    assert episode_count is not None

    # Equal coverage is already one metadata title.  A larger provider count is
    # a completeness concern, not evidence that this local title must split.
    if episode_count >= len(logical_videos):
        return None

    if (
        title.episode_start is not None
        or title.episode_end is not None
        or title.episode_filename_pattern
    ):
        return _ambiguous(
            title, metadata, primary,
            "Lokální část používá range/pattern manual-split authority; metadata "
            "subset ji nesmí automaticky přepsat ani překrýt.",
        )
    if any(video.media_part_number is not None for video in title.videos):
        return _ambiguous(
            title, metadata, primary,
            "Lokální skupina používá Media Part; počet fyzických souborů proto "
            "nelze bezpečně porovnat s počtem metadata epizod.",
        )
    if len(logical_videos) != len(title.videos):
        return _ambiguous(
            title, metadata, primary,
            "Lokální skupina obsahuje potvrzené nebo poškozené duplicate vazby; "
            "subset se bez dalšího rozhodnutí nepřesouvá.",
        )

    positions = [_safe_metadata_position(video, title) for video in logical_videos]
    if any(position is None for position in positions):
        return _ambiguous(
            title, metadata, primary,
            "Metadata pokrývají méně položek než lokální skupina, ale nejméně "
            "jedno video nemá bezpečné číslo pro metadata mapping.",
        )
    typed_positions = [position for position in positions if position is not None]
    signatures = {signature for signature, _ in typed_positions}
    ordinals = [ordinal for _, ordinal in typed_positions]
    if (
        len(signatures) != 1
        or len(set(ordinals)) != len(ordinals)
        or set(ordinals) != set(range(1, len(logical_videos) + 1))
    ):
        return _ambiguous(
            title, metadata, primary,
            "Metadata episode_count ukazuje na subset, ale lokální videa netvoří "
            "jednu úplnou a jednoznačnou číselnou řadu 1..N.",
        )

    numbered_videos = sorted(
        zip(logical_videos, typed_positions), key=lambda item: item[1][1]
    )
    matching = tuple(
        video for video, (_, ordinal) in numbered_videos
        if ordinal <= episode_count
    )
    remaining = tuple(
        video for video, (_, ordinal) in numbered_videos
        if ordinal > episode_count
    )
    if len(matching) != episode_count or not remaining:
        return _ambiguous(
            title, metadata, primary,
            "Potvrzený metadata rozsah nelze jednoznačně převést na neprázdný "
            "subset lokálních videí.",
        )
    return MetadataSplitEvaluation(
        MetadataSplitStatus.RECOMMENDED,
        title,
        metadata,
        primary,
        matching,
        remaining,
    )


def _metadata_split_local_title(
    title: CatalogTitle, metadata: TitleMetadata,
) -> str:
    type_label = PART_TYPE_LABELS.get(title.effective_part_type, "").strip()
    metadata_title = metadata.display_title.strip()
    if not type_label:
        return metadata_title[:200]
    return f"{type_label} – {metadata_title}"[:200]


def _load_split_source(session: Session, title_id: int) -> CatalogTitle:
    title = session.scalar(select(CatalogTitle).options(
        selectinload(CatalogTitle.videos).selectinload(Video.manual_split_rule_videos),
        selectinload(CatalogTitle.external_links),
        selectinload(CatalogTitle.metadata_record),
        selectinload(CatalogTitle.metadata_candidates),
        selectinload(CatalogTitle.artwork),
        selectinload(CatalogTitle.collection).selectinload(
            CatalogCollection.titles
        ).selectinload(CatalogTitle.manual_split_rule_videos),
        selectinload(CatalogTitle.collection).selectinload(CatalogCollection.videos),
    ).where(CatalogTitle.id == title_id))
    if title is None:
        raise ValueError("Titul nebyl nalezen.")
    return title


def _move_confirmed_metadata(
    source: CatalogTitle,
    target: CatalogTitle,
    evaluation: MetadataSplitEvaluation,
) -> None:
    metadata = evaluation.metadata
    primary = evaluation.primary_link
    metadata.catalog_title_id = target.id
    primary.catalog_title_id = target.id

    for candidate in source.metadata_candidates:
        if (
            candidate.provider == primary.provider
            and candidate.external_id == primary.external_id
            and candidate.confirmed_at is not None
        ):
            candidate.catalog_title_id = target.id
    for artwork in source.artwork:
        if (
            artwork.provider == primary.provider
            and artwork.external_id == primary.external_id
        ):
            artwork.catalog_title_id = target.id

    target.preferred_metadata_provider = primary.provider
    target.preferred_external_id = primary.external_id
    target.metadata_status = "linked_manual"
    target.metadata_locked = source.metadata_locked
    source.preferred_metadata_provider = None
    source.preferred_external_id = None
    source.metadata_locked = False
    source.metadata_status = (
        "candidates_available"
        if any(
            candidate.rejected_at is None
            and not (
                candidate.provider == primary.provider
                and candidate.external_id == primary.external_id
                and candidate.confirmed_at is not None
            )
            for candidate in source.metadata_candidates
        )
        else "unlinked"
    )


def apply_metadata_split(
    session: Session,
    title_id: int,
    *,
    confirmed: bool,
) -> MetadataSplitResult:
    """Apply a freshly revalidated metadata split after explicit confirmation."""
    if not confirmed:
        raise ValueError("Rozdělení podle potvrzených metadat je nutné potvrdit.")
    source = _load_split_source(session, title_id)
    evaluation = evaluate_metadata_split(source)
    if evaluation is not None and evaluation.is_ambiguous:
        raise ValueError(evaluation.reason or "Metadata mapping není jednoznačný.")
    if evaluation is None or not evaluation.is_recommendation:
        raise ValueError(
            "Titul už nesplňuje bezpečné podmínky metadata-driven splitu."
        )
    collection = source.collection
    if collection is None:
        raise ValueError("Titul není přiřazený ke kolekci.")

    moved = evaluation.matching_videos
    new_title = create_title_from_videos(
        session,
        collection.id,
        [video.id for video in moved],
        local_title=evaluation.proposed_local_title,
        part_type=source.effective_part_type,
        season_number=source.effective_season_number,
        season_label=source.effective_season_label,
        part_number=source.effective_part_number,
    )

    # Existing range/pattern authorities remain authoritative.  If the new
    # explicit selector would overlap any of them, abort the whole transaction
    # instead of silently weakening an older manual decision.
    manual_split = evaluate_persisted_manual_split(collection, list(collection.videos))
    moved_ids = {video.id for video in moved}
    invalid_decisions = [
        decision
        for decision in manual_split.decisions
        if decision.video.id in moved_ids
        and (
            decision.kind != ManualSplitDecisionKind.UNIQUE
            or decision.target_catalog_title is not new_title
        )
    ]
    if invalid_decisions:
        raise ValueError(
            "Metadata split je v konfliktu s existující manual-split authority; "
            "nejprve upravte hierarchy pravidla."
        )

    _move_confirmed_metadata(source, new_title, evaluation)
    session.flush()
    remaining = tuple(
        sorted(source.videos, key=lambda video: (video.relative_path.casefold(), video.id or 0))
    )
    recalculate_title_numbering(source, list(remaining), external_linked=False)
    recalculate_title_numbering(new_title, list(moved), external_linked=True)
    refresh_collection_state(collection)
    session.flush()
    return MetadataSplitResult(source, new_title, moved, remaining)
