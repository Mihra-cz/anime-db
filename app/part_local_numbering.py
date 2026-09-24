"""Explicit Part-local numbering authority: read-only proposal and atomic apply.

A CatalogTitle with an explicit Part keeps its own standard namespace E01..EN
once a human confirms ``numbering_mode=part_local``.  Part structure alone never
renumbers.  This module only *proposes* the confirmation when the current
canonical identities can be moved mechanically, and applies exactly a freshly
revalidated proposal.  Anything that would need a guess is reported as Review.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json

from sqlalchemy.orm import Session

from .catalog import effective_video_content_type
from .models import CatalogTitle, Video
from .numbering import (
    PART_LOCAL_NUMBERING_MODE,
    SUPPLEMENTAL_PART_TYPES,
    DuplicateRelationState,
    _default_title_issues,
    _issues_for_title_from_evaluation,
    collapses_into_duplicate_primary,
    collection_numbering_bases,
    deterministic_video_order_key,
    duplicate_relation_state,
    effective_recap_episode_number,
    effective_video_numbering,
    loaded_duplicate_primary,
    logical_episode_partitions,
    set_title_numbering,
    summarize_title_numbering,
    unresolved_duplicate_groups,
)

EXISTING_AUTHORITY_REASON = (
    "Část už má explicitní title-level numbering autoritu (režim nebo offset); "
    "automatický Part-lokální návrh ji nepřepisuje."
)
HIERARCHY_ISSUE_REASON = (
    "Část má aktivní hierarchy/numbering problém; Part-lokální návrh vznikne "
    "až po jeho vyřešení."
)
AMBIGUOUS_IDENTITY_REASON = (
    "Standardní logické identity nejsou jednoznačné (neznámé, nestandardní, "
    "nevyřešené duplicity nebo neplatné duplicate vazby)."
)
SEQUENCE_REASON = "Standardní řada nemá souvislé logické identity bez mezer a kolizí."
MANUAL_CONFLICT_REASON = (
    "Existující ruční číslo videa není mechanicky kompatibilní s Part-lokální "
    "řadou; ruční autorita se nepřepisuje."
)
SOURCE_OFFSET_REASON = (
    "Zdrojová čísla nelze převést jedním konstantním nezáporným offsetem."
)
RECAP_REASON = (
    "Recap by vyžadoval převod ze sezónní na Part-lokální souřadnici; "
    "jeho pozici je nutné posoudit ručně."
)


@dataclass(frozen=True)
class PartLocalNumberingRow:
    current_episode: int
    proposed_episode: int
    video_ids: tuple[int, ...]
    filenames: tuple[str, ...]
    physical_count: int
    duplicate_copy_count: int
    absolute_before: tuple[int | None, ...]
    absolute_after: int | None
    external_before: tuple[int | None, ...]
    external_after: int | None


@dataclass(frozen=True)
class PartLocalNumberingProposal:
    catalog_title_id: int
    title_name: str
    season_number: int | None
    part_number: int
    current_mode: str
    current_offset: int | None
    source_offset: int
    rows: tuple[PartLocalNumberingRow, ...]
    physical_video_count: int
    absolute_base: int | None
    warnings: tuple[str, ...]
    fingerprint: str

    proposed_mode = PART_LOCAL_NUMBERING_MODE

    @property
    def logical_episode_count(self) -> int:
        return len(self.rows)

    @property
    def changes_canonical(self) -> bool:
        return any(row.current_episode != row.proposed_episode for row in self.rows)

    @property
    def mapping_label(self) -> str:
        first, last = self.rows[0], self.rows[-1]
        return (
            f"E{first.current_episode}–E{last.current_episode} → "
            f"E{first.proposed_episode}–E{last.proposed_episode}"
        )


@dataclass(frozen=True)
class PartLocalNumberingEvaluation:
    proposal: PartLocalNumberingProposal | None = None
    review_reason: str | None = None


def _review(reason: str) -> PartLocalNumberingEvaluation:
    return PartLocalNumberingEvaluation(review_reason=reason)


def _fingerprint(title: CatalogTitle, source_offset: int, rows: list[tuple]) -> str:
    collection = title.collection
    titles = sorted(collection.titles, key=lambda item: item.id or 0)
    payload = (
        title.id,
        title.catalog_collection_id,
        title.effective_part_type,
        title.effective_season_number,
        title.effective_part_number,
        title.numbering_mode,
        title.episode_start_offset,
        source_offset,
        [
            (
                item.id, item.effective_part_type, item.effective_season_number,
                item.effective_part_number, item.effective_sort_order,
                item.metadata_record.episode_count if item.metadata_record else None,
            )
            for item in titles
        ],
        [
            (
                video.id, video.catalog_title_id, video.catalog_collection_id,
                video.relative_path, video.file_type, video.content_type_manual,
                video.local_episode_number, video.season_episode_number,
                video.absolute_episode_number, video.external_episode_number,
                video.episode_number_manual_override,
                video.recap_episode_number_manual_tenths,
                video.video_variant_group_id, video.duplicate_of_video_id,
                bool(video.duplicate_primary_missing), video.media_part_number,
            )
            for video in sorted(title.videos, key=lambda item: (item.id or 0, item.relative_path))
        ],
        rows,
    )
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _absolute_base(title: CatalogTitle) -> int | None:
    """Base the collection pass would use once this title is part_local."""
    bases = collection_numbering_bases(title.collection, assume_part_local=title)
    return next((known for item, known in bases if item is title), None)


def evaluate_part_local_numbering(
    title: CatalogTitle,
    *,
    issues: tuple[object, ...] | None = None,
) -> PartLocalNumberingEvaluation | None:
    """Return a no-write proposal, a Review reason, or None when not applicable.

    Only a title with an explicit Part and without any title-level numbering
    authority is considered.  Logical slots come from current canonical
    identities, never from file order; the confirmed source offset is the
    single constant that maps every parser number onto E01..EN.
    """
    if (
        title.id is None
        or title.collection is None
        or title.effective_part_number is None
        or title.effective_part_type in SUPPLEMENTAL_PART_TYPES
        or title.numbering_mode == PART_LOCAL_NUMBERING_MODE
    ):
        return None
    if title.numbering_mode != "unknown" or title.episode_start_offset is not None:
        return _review(EXISTING_AUTHORITY_REASON)

    title_issues = _default_title_issues(title) if issues is None else tuple(issues)
    if any(bool(getattr(issue, "blocking", False)) for issue in title_issues):
        return _review(HIERARCHY_ISSUE_REASON)

    videos = list(title.videos)
    summary = summarize_title_numbering(videos, title)
    if (
        summary.unnumbered_standard or summary.unknown or summary.nonstandard
        or summary.duplicate_numbers or summary.invalid_duplicate_references
        or summary.variant_inconsistent_confirmed_duplicates
        or summary.identity_inconsistent_confirmed_duplicates
        or summary.unverifiable_confirmed_duplicates
        or unresolved_duplicate_groups(videos, catalog_title=title)
    ):
        return _review(AMBIGUOUS_IDENTITY_REASON)

    partitions = logical_episode_partitions(videos, catalog_title=title)
    if not partitions:
        return None
    numbers = [partition.identity.season_episode_number for partition in partitions]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return _review(SEQUENCE_REASON)
    shift = numbers[0] - 1
    proposed_by_current = {number: number - shift for number in numbers}

    known_videos = {video.id: video for video in videos if video.id is not None}
    members: dict[int, list[Video]] = {number: list(partition.videos) for number, partition in zip(numbers, partitions)}
    duplicate_counts = {number: 0 for number in numbers}
    for video in videos:
        if not collapses_into_duplicate_primary(video, known_videos=known_videos):
            continue
        primary = loaded_duplicate_primary(video, known_videos=known_videos)
        current = primary.season_episode_number if primary is not None else None
        if current not in members:
            return _review(AMBIGUOUS_IDENTITY_REASON)
        members[current].append(video)
        duplicate_counts[current] += 1

    source_offsets: set[int] = set()
    for current, slot_videos in members.items():
        proposed = proposed_by_current[current]
        for video in slot_videos:
            if video.episode_number_manual_override is not None:
                # In part_local an explicit video number is already Part-local.
                if video.episode_number_manual_override != proposed:
                    return _review(MANUAL_CONFLICT_REASON)
                continue
            source = effective_video_numbering(video, title).numbering_input
            if source is None:
                return _review(AMBIGUOUS_IDENTITY_REASON)
            source_offsets.add(source - proposed)
    if len(source_offsets) > 1 or any(offset < 0 for offset in source_offsets):
        return _review(SOURCE_OFFSET_REASON)
    source_offset = next(iter(source_offsets), 0)

    for video in videos:
        if effective_video_content_type(video, title) != "recap":
            continue
        position = effective_recap_episode_number(video, title)
        if position is None:
            continue
        if shift or not (Decimal(0) < position < Decimal(len(numbers) + 1)):
            return _review(RECAP_REASON)

    absolute_base = _absolute_base(title)
    has_external = title.metadata_record is not None
    rows: list[PartLocalNumberingRow] = []
    fingerprint_rows: list[tuple] = []
    for current in numbers:
        proposed = proposed_by_current[current]
        slot = sorted(members[current], key=deterministic_video_order_key)
        row = PartLocalNumberingRow(
            current_episode=current,
            proposed_episode=proposed,
            video_ids=tuple(video.id for video in slot),
            filenames=tuple(video.filename for video in slot),
            physical_count=len(slot) - duplicate_counts[current],
            duplicate_copy_count=duplicate_counts[current],
            absolute_before=tuple(sorted(
                {video.absolute_episode_number for video in slot},
                key=lambda value: (value is None, value or 0),
            )),
            absolute_after=(
                absolute_base + proposed if absolute_base is not None else None
            ),
            external_before=tuple(sorted(
                {video.external_episode_number for video in slot},
                key=lambda value: (value is None, value or 0),
            )),
            external_after=proposed if has_external else None,
        )
        rows.append(row)
        fingerprint_rows.append((current, proposed, row.video_ids))

    warnings = tuple(message for message in (
        (
            "Canonical čísla se nemění; návrh pouze ukotví současné číslování "
            "explicitní Part-lokální autoritou."
            if not shift else None
        ),
        (
            "Počet epizod předchozích částí není bezpečně známý; absolutní "
            "projekce zůstane prázdná."
            if absolute_base is None else None
        ),
        (
            "Existující ruční čísla videí zůstanou beze změny; v Part-lokálním "
            "režimu jsou přímo Part-lokálními čísly."
            if any(video.episode_number_manual_override is not None for video in videos)
            else None
        ),
    ) if message is not None)
    return PartLocalNumberingEvaluation(proposal=PartLocalNumberingProposal(
        catalog_title_id=title.id,
        title_name=title.local_title,
        season_number=title.effective_season_number,
        part_number=title.effective_part_number,
        current_mode=title.numbering_mode,
        current_offset=title.episode_start_offset,
        source_offset=source_offset,
        rows=tuple(rows),
        physical_video_count=sum(len(members[current]) for current in numbers),
        absolute_base=absolute_base,
        warnings=warnings,
        fingerprint=_fingerprint(title, source_offset, fingerprint_rows),
    ))


def apply_part_local_numbering(
    session: Session,
    catalog_title_id: int,
    *,
    expected_fingerprint: str,
) -> PartLocalNumberingProposal:
    """Revalidate and atomically store one freshly confirmed proposal."""
    title = session.get(CatalogTitle, catalog_title_id)
    if title is None or title.collection is None:
        raise ValueError("Část pro Part-lokální číslování nebyla nalezena.")
    collection = title.collection

    from .hierarchy_evaluation import (
        evaluate_collection_hierarchy,
        finalize_hierarchy_write,
        strict_hierarchy_write_guard,
    )

    evaluation = evaluate_collection_hierarchy(
        collection, list(collection.videos), include_legacy_fallback=False,
    )
    result = evaluate_part_local_numbering(
        title, issues=_issues_for_title_from_evaluation(title, evaluation.issues),
    )
    proposal = result.proposal if result is not None else None
    if (
        proposal is None
        or not expected_fingerprint
        or proposal.fingerprint != expected_fingerprint
    ):
        raise ValueError(
            "Náhled Part-lokálního číslování je zastaralý nebo již není "
            "jednoznačný; načtěte nový návrh."
        )

    other_numbers = {
        video.id: video.season_episode_number
        for video in collection.videos if video.catalog_title_id != title.id
    }
    authority_before = {
        video.id: (
            video.episode_number_manual_override,
            video.recap_episode_number_manual_tenths,
            video.media_part_number,
            video.video_variant_group_id,
            video.duplicate_of_video_id,
            video.content_type_manual,
        )
        for video in title.videos
    }
    valid_secondaries = [
        video for video in title.videos
        if duplicate_relation_state(video) == DuplicateRelationState.VALID
    ]
    with strict_hierarchy_write_guard(session, [collection]):
        set_title_numbering(title, PART_LOCAL_NUMBERING_MODE, proposal.source_offset)
        session.flush()
        finalize_hierarchy_write([collection])
        session.flush()

        videos_by_id = {video.id: video for video in title.videos}
        for row in proposal.rows:
            for video_id in row.video_ids:
                video = videos_by_id.get(video_id)
                if (
                    video is None
                    or video.season_episode_number != row.proposed_episode
                    or video.absolute_episode_number != row.absolute_after
                    or video.external_episode_number != row.external_after
                ):
                    raise ValueError(
                        "Výsledné číslování neodpovídá potvrzenému náhledu; "
                        "operace byla vrácena zpět."
                    )
        final_numbers = [
            partition.identity.season_episode_number
            for partition in logical_episode_partitions(
                list(title.videos), catalog_title=title,
            )
        ]
        if final_numbers != list(range(1, len(proposal.rows) + 1)):
            raise ValueError(
                "Výsledná Part-lokální řada není E01..EN; operace byla vrácena zpět."
            )
        if unresolved_duplicate_groups(list(title.videos), catalog_title=title) or any(
            duplicate_relation_state(video) != DuplicateRelationState.VALID
            for video in valid_secondaries
        ):
            raise ValueError(
                "Přečíslování by porušilo potvrzenou duplicitu nebo vytvořilo "
                "kolizi; operace byla vrácena zpět."
            )
        if other_numbers != {
            video.id: video.season_episode_number
            for video in collection.videos if video.catalog_title_id != title.id
        } or authority_before != {
            video.id: (
                video.episode_number_manual_override,
                video.recap_episode_number_manual_tenths,
                video.media_part_number,
                video.video_variant_group_id,
                video.duplicate_of_video_id,
                video.content_type_manual,
            )
            for video in title.videos
        }:
            raise ValueError(
                "Operace by změnila jinou část nebo per-video autoritu; "
                "byla vrácena zpět."
            )
    return proposal
