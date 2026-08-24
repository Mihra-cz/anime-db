from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .catalog import (
    is_root_video,
    meaningful_root_collection,
    normalize_title,
)
from .hierarchy import CollectionIdentity, HierarchyIdentity, TitleIdentity, derive_library_hierarchy
from .hierarchy_authority import manual_hierarchy_snapshot_requires_preservation
from .hierarchy_evaluation import HierarchyEvaluationResult, finalize_collection_hierarchy
from .hierarchy_review import extract_local_period_hint
from .manual_split import (
    ManualSplitDecisionKind,
    ManualSplitVideoDecision,
    compile_manual_split_pattern,
    evaluate_persisted_manual_split,
    has_persisted_manual_split_selector,
    historical_manual_split_ambiguities,
    manual_split_titles,
)
from .models import (
    CatalogCollection,
    CatalogTitle,
    ManualSplitRuleVideo,
    TitleMetadata,
    Video,
)
from .numbering import effective_video_numbering, is_nonprimary_duplicate_video


class ReconciliationAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    PRESERVE = "preserve"
    REMOVE = "remove"


class ReconciliationReason(StrEnum):
    AUTOMATIC_PATH = "automatic_path"
    MANUAL_CURRENT_ASSIGNMENT = "manual_current_assignment"
    MANUAL_PATH_AUTHORITY = "manual_path_authority"
    MANUAL_SPLIT_UNIQUE = "manual_split_unique"
    MANUAL_SPLIT_CONFLICT = "manual_split_conflict"
    MANUAL_SPLIT_UNMATCHED = "manual_split_unmatched"
    ROOT_UNASSIGNED = "root_unassigned"
    ROOT_PRESERVED = "root_preserved"
    AUTOMATIC_OBSOLETE = "automatic_obsolete"
    PROTECTED_USER_DATA = "protected_user_data"
    CURRENT = "current"


@dataclass(frozen=True)
class CollectionSnapshot:
    relative_root_path: str
    local_title: str
    normalized_local_title: str
    local_period_hint: str | None
    hierarchy_status: str
    hierarchy_note: str | None
    hierarchy_verified: bool


@dataclass(frozen=True)
class TitleSnapshot:
    relative_root_path: str
    collection_path: str | None
    local_title: str
    normalized_local_title: str
    part_type: str
    season_number: int | None
    part_number: int | None
    season_label: str | None
    original_folder_name: str | None
    sort_order: int


@dataclass(frozen=True)
class VideoNumberingSnapshot:
    local_episode_number: int | None
    season_episode_number: int | None
    absolute_episode_number: int | None
    external_episode_number: int | None
    episode_number_source: str
    episode_number_confidence: float | None


@dataclass(frozen=True)
class CollectionPlanItem:
    action: ReconciliationAction
    collection_id: int | None
    relative_root_path: str
    current: CollectionSnapshot | None
    desired: CollectionSnapshot | None
    reason: ReconciliationReason


@dataclass(frozen=True)
class TitlePlanItem:
    action: ReconciliationAction
    title_id: int | None
    relative_root_path: str
    current: TitleSnapshot | None
    desired: TitleSnapshot | None
    protected: bool
    protection_reasons: tuple[str, ...]
    reason: ReconciliationReason


@dataclass(frozen=True)
class VideoAssignmentPlanItem:
    video_id: int
    relative_path: str
    old_collection_path: str | None
    old_title_path: str | None
    target_collection_path: str | None
    target_title_path: str | None
    manual_split_kind: str | None
    matched_manual_title_ids: tuple[int, ...]
    matched_manual_title_paths: tuple[str, ...]
    reason: ReconciliationReason

    @property
    def changed(self) -> bool:
        return (
            self.old_collection_path,
            self.old_title_path,
        ) != (
            self.target_collection_path,
            self.target_title_path,
        )


@dataclass(frozen=True)
class VideoNumberingPlanItem:
    video_id: int
    relative_path: str
    current: VideoNumberingSnapshot
    desired: VideoNumberingSnapshot


@dataclass(frozen=True)
class HierarchyIssuePlanItem:
    collection_path: str
    code: str
    blocking: bool
    scope: str
    title_path: str | None
    video_paths: tuple[str, ...]
    related_title_paths: tuple[str, ...]


@dataclass(frozen=True)
class RebuildBlocker:
    code: str
    collection_path: str | None = None
    title_path: str | None = None
    video_path: str | None = None
    prevents_apply: bool = False


@dataclass(frozen=True)
class RebuildSummary:
    collections_created: int
    collections_updated: int
    collections_preserved: int
    collections_removed: int
    titles_created: int
    titles_updated: int
    titles_preserved: int
    titles_removed: int
    protected_titles: int
    video_assignments_changed: int
    numbering_changes: int
    conflicts: int
    blocking_issues: int

    @property
    def logical_changes(self) -> int:
        return sum((
            self.collections_created,
            self.collections_updated,
            self.collections_removed,
            self.titles_created,
            self.titles_updated,
            self.titles_removed,
            self.video_assignments_changed,
            self.numbering_changes,
        ))


@dataclass(frozen=True)
class HierarchyRebuildPlan:
    source_fingerprint: str
    collections: tuple[CollectionPlanItem, ...]
    titles: tuple[TitlePlanItem, ...]
    video_assignments: tuple[VideoAssignmentPlanItem, ...]
    numbering: tuple[VideoNumberingPlanItem, ...]
    issues: tuple[HierarchyIssuePlanItem, ...]
    blockers: tuple[RebuildBlocker, ...]

    @property
    def summary(self) -> RebuildSummary:
        collection_counts = _action_counts(item.action for item in self.collections)
        title_counts = _action_counts(item.action for item in self.titles)
        return RebuildSummary(
            collections_created=collection_counts[ReconciliationAction.CREATE],
            collections_updated=collection_counts[ReconciliationAction.UPDATE],
            collections_preserved=collection_counts[ReconciliationAction.PRESERVE],
            collections_removed=collection_counts[ReconciliationAction.REMOVE],
            titles_created=title_counts[ReconciliationAction.CREATE],
            titles_updated=title_counts[ReconciliationAction.UPDATE],
            titles_preserved=title_counts[ReconciliationAction.PRESERVE],
            titles_removed=title_counts[ReconciliationAction.REMOVE],
            protected_titles=sum(item.protected for item in self.titles),
            video_assignments_changed=sum(item.changed for item in self.video_assignments),
            numbering_changes=len(self.numbering),
            conflicts=sum(item.code == "manual_split_conflict" for item in self.issues),
            blocking_issues=sum(item.blocking for item in self.issues),
        )

    @property
    def has_changes(self) -> bool:
        return self.summary.logical_changes > 0

    @property
    def affected_collection_paths(self) -> tuple[str, ...]:
        paths = {
            item.relative_root_path
            for item in self.collections
            if item.action != ReconciliationAction.PRESERVE
        }
        paths.update(
            path
            for item in self.video_assignments
            if item.changed
            for path in (item.old_collection_path, item.target_collection_path)
            if path is not None
        )
        paths.update(
            item.desired.collection_path
            for item in self.titles
            if item.action != ReconciliationAction.PRESERVE
            and item.desired is not None
            and item.desired.collection_path is not None
        )
        return tuple(sorted(paths))


@dataclass(frozen=True)
class HierarchyRebuildResult:
    plan: HierarchyRebuildPlan
    applied: bool

    @property
    def summary(self) -> RebuildSummary:
        return self.plan.summary


class HierarchyRebuildError(RuntimeError):
    pass


class HierarchyPlanStaleError(HierarchyRebuildError):
    pass


class HierarchyPlanBlockedError(HierarchyRebuildError):
    pass


@dataclass(frozen=True)
class _TitleSpec:
    original: CatalogTitle | None
    identity: TitleIdentity | None
    collection_path: str | None
    protected: bool
    protection_reasons: tuple[str, ...]


@dataclass(frozen=True)
class _AssignmentIntent:
    collection_path: str | None
    title_path: str | None
    decision: ManualSplitVideoDecision | None
    reason: ReconciliationReason


@dataclass
class _Projection:
    collections: dict[str, CatalogCollection]
    titles: dict[str, CatalogTitle]
    videos: dict[int, Video]
    evaluations: dict[str, HierarchyEvaluationResult]


def _action_counts(actions: Iterable[ReconciliationAction]) -> dict[ReconciliationAction, int]:
    counts = {action: 0 for action in ReconciliationAction}
    for action in actions:
        counts[action] += 1
    return counts


def _require_clean_unit_of_work(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise HierarchyRebuildError(
            "Hierarchy reconciliation vyžaduje session bez pending změn; "
            "nejprve je explicitně flushněte nebo dokončete vlastní transakci."
        )


def _load_state(
    session: Session,
) -> tuple[list[CatalogCollection], list[CatalogTitle], list[Video]]:
    with session.no_autoflush:
        collections = list(session.scalars(
            select(CatalogCollection).options(
                selectinload(CatalogCollection.titles).selectinload(
                    CatalogTitle.metadata_record
                ),
                selectinload(CatalogCollection.titles).selectinload(
                    CatalogTitle.external_links
                ),
                selectinload(CatalogCollection.titles).selectinload(
                    CatalogTitle.metadata_candidates
                ),
                selectinload(CatalogCollection.titles).selectinload(
                    CatalogTitle.artwork
                ),
                selectinload(CatalogCollection.titles).selectinload(
                    CatalogTitle.manual_split_rule_videos
                ),
                selectinload(CatalogCollection.videos),
            ).order_by(CatalogCollection.relative_root_path)
        ))
        titles = list(session.scalars(
            select(CatalogTitle).options(
                selectinload(CatalogTitle.metadata_record),
                selectinload(CatalogTitle.external_links),
                selectinload(CatalogTitle.metadata_candidates),
                selectinload(CatalogTitle.artwork),
                selectinload(CatalogTitle.manual_split_rule_videos),
                selectinload(CatalogTitle.videos),
                selectinload(CatalogTitle.collection),
            ).order_by(CatalogTitle.relative_root_path)
        ))
        videos = list(session.scalars(
            select(Video).options(
                selectinload(Video.catalog_title).selectinload(CatalogTitle.collection),
                selectinload(Video.catalog_collection),
            ).order_by(Video.relative_path)
        ))
    return collections, titles, videos


def _state_fingerprint(
    collections: list[CatalogCollection],
    titles: list[CatalogTitle],
    videos: list[Video],
) -> str:
    payload = {
        "collections": [
            (
                item.id,
                item.relative_root_path,
                item.local_title,
                item.normalized_local_title,
                item.manual_display_title,
                item.hierarchy_status,
                item.hierarchy_verified_at.isoformat() if item.hierarchy_verified_at else None,
                item.hierarchy_note,
                item.local_period_hint,
            )
            for item in collections
        ],
        "titles": [
            (
                item.id,
                item.catalog_collection_id,
                item.relative_root_path,
                item.local_title,
                item.normalized_local_title,
                item.part_type,
                item.season_number,
                item.part_number,
                item.season_label,
                item.original_folder_name,
                item.sort_order,
                item.episode_start_offset,
                item.numbering_mode,
                item.numbering_manual,
                item.numbering_verified_at.isoformat() if item.numbering_verified_at else None,
                item.hierarchy_manual_override,
                item.season_number_manual,
                item.part_number_manual,
                item.season_label_manual,
                item.part_type_manual,
                item.sort_order_manual,
                item.hierarchy_verified_at.isoformat() if item.hierarchy_verified_at else None,
                item.episode_start,
                item.episode_end,
                item.episode_filename_pattern,
                item.manual_display_title,
                item.preferred_metadata_provider,
                item.preferred_external_id,
                item.metadata_status,
                item.metadata_locked,
                (
                    (
                        item.metadata_record.display_title,
                        item.metadata_record.title_romaji,
                        item.metadata_record.title_english,
                        item.metadata_record.title_native,
                        item.metadata_record.description,
                        item.metadata_record.release_year,
                        item.metadata_record.season,
                        item.metadata_record.format,
                        item.metadata_record.status,
                        item.metadata_record.episode_count,
                        item.metadata_record.episode_duration_minutes,
                        item.metadata_record.genres_json,
                        item.metadata_record.tags_json,
                        item.metadata_record.synonyms_json,
                        item.metadata_record.country_of_origin,
                        item.metadata_record.is_adult,
                        item.metadata_record.metadata_provider,
                        item.metadata_record.metadata_external_id,
                        item.metadata_record.cover_image_url,
                        item.metadata_record.metadata_fetched_at.isoformat()
                        if item.metadata_record.metadata_fetched_at else None,
                        item.metadata_record.metadata_updated_at.isoformat()
                        if item.metadata_record.metadata_updated_at else None,
                    )
                    if item.metadata_record is not None else None
                ),
                tuple(sorted(link.id for link in item.external_links)),
                tuple(sorted(candidate.id for candidate in item.metadata_candidates)),
                tuple(sorted(artwork.id for artwork in item.artwork)),
                tuple(sorted(link.video_id for link in item.manual_split_rule_videos)),
            )
            for item in titles
        ],
        "videos": [
            (
                item.id,
                item.relative_path,
                item.filename,
                item.catalog_collection_id,
                item.catalog_title_id,
                item.local_episode_number,
                item.season_episode_number,
                item.absolute_episode_number,
                item.external_episode_number,
                item.episode_number_source,
                item.episode_number_confidence,
                item.episode_number_manual_override,
                item.episode_number_verified_at.isoformat()
                if item.episode_number_verified_at else None,
                item.content_type_manual,
                item.media_part_number,
                item.duplicate_status_manual,
                item.duplicate_of_video_id,
                item.duplicate_primary_missing,
            )
            for item in videos
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _title_protection_reasons(title: CatalogTitle) -> tuple[str, ...]:
    reasons: list[str] = []
    if title.hierarchy_manual_override:
        reasons.append("hierarchy_manual_override")
    if title.hierarchy_verified_at is not None:
        reasons.append("hierarchy_verified_at")
    if any(
        value is not None
        for value in (
            title.season_number_manual,
            title.part_number_manual,
            title.season_label_manual,
            title.part_type_manual,
            title.sort_order_manual,
        )
    ):
        reasons.append("manual_hierarchy_fields")
    if title.manual_split_rule_videos:
        reasons.append("manual_split_video_authority")
    if any(value is not None for value in (title.episode_start, title.episode_end)):
        reasons.append("manual_split_range")
    if title.episode_filename_pattern is not None:
        reasons.append("manual_split_pattern")
    if title.manual_display_title is not None:
        reasons.append("manual_display_title")
    if title.metadata_record is not None:
        reasons.append("metadata")
    if title.external_links:
        reasons.append("external_links")
    if title.metadata_candidates:
        reasons.append("metadata_candidates")
    if title.artwork:
        reasons.append("artwork")
    if any(
        value is not None
        for value in (title.preferred_metadata_provider, title.preferred_external_id)
    ):
        reasons.append("metadata_preference")
    if title.metadata_locked or title.metadata_status != "unlinked":
        reasons.append("metadata_state")
    if (
        title.numbering_manual
        or title.numbering_verified_at is not None
        or title.numbering_mode != "unknown"
        or title.episode_start_offset is not None
    ):
        reasons.append("manual_numbering")
    return tuple(reasons)


def _collection_has_user_state(collection: CatalogCollection) -> bool:
    return bool(
        collection.manual_display_title is not None
        or collection.hierarchy_verified_at is not None
        or collection.hierarchy_status != "automatic"
        or collection.hierarchy_note is not None
    )


def _collection_snapshot(collection: CatalogCollection) -> CollectionSnapshot:
    return CollectionSnapshot(
        relative_root_path=collection.relative_root_path,
        local_title=collection.local_title,
        normalized_local_title=collection.normalized_local_title,
        local_period_hint=collection.local_period_hint,
        hierarchy_status=collection.hierarchy_status,
        hierarchy_note=collection.hierarchy_note,
        hierarchy_verified=collection.hierarchy_verified_at is not None,
    )


def _title_snapshot(title: CatalogTitle) -> TitleSnapshot:
    return TitleSnapshot(
        relative_root_path=title.relative_root_path,
        collection_path=(
            title.collection.relative_root_path if title.collection is not None else None
        ),
        local_title=title.local_title,
        normalized_local_title=title.normalized_local_title,
        part_type=title.part_type,
        season_number=title.season_number,
        part_number=title.part_number,
        season_label=title.season_label,
        original_folder_name=title.original_folder_name,
        sort_order=title.sort_order,
    )


def _numbering_snapshot(video: Video) -> VideoNumberingSnapshot:
    return VideoNumberingSnapshot(
        local_episode_number=video.local_episode_number,
        season_episode_number=video.season_episode_number,
        absolute_episode_number=video.absolute_episode_number,
        external_episode_number=video.external_episode_number,
        episode_number_source=video.episode_number_source,
        episode_number_confidence=video.episode_number_confidence,
    )


def _current_collection_path(video: Video) -> str | None:
    return (
        video.catalog_collection.relative_root_path
        if video.catalog_collection is not None else None
    )


def _current_title_path(video: Video) -> str | None:
    return video.catalog_title.relative_root_path if video.catalog_title is not None else None


def _manual_assignment_candidate(
    video: Video,
    identity: HierarchyIdentity,
    titles_by_path: dict[str, CatalogTitle],
) -> tuple[CatalogTitle | None, ReconciliationReason | None]:
    current = video.catalog_title
    if (
        current is not None
        and manual_hierarchy_snapshot_requires_preservation(current)
        and current.collection is not None
    ):
        return current, ReconciliationReason.MANUAL_CURRENT_ASSIGNMENT
    path_title = titles_by_path.get(identity.title.relative_root_path)
    if (
        path_title is not None
        and manual_hierarchy_snapshot_requires_preservation(path_title)
        and path_title.collection is not None
        and path_title.collection.relative_root_path
        != identity.collection.relative_root_path
    ):
        return path_title, ReconciliationReason.MANUAL_PATH_AUTHORITY
    return None, None


def _manual_split_title_blockers(
    collection: CatalogCollection,
) -> tuple[tuple[CatalogTitle, str], ...]:
    blockers: list[tuple[CatalogTitle, str]] = []
    for title in manual_split_titles(collection):
        start, end = title.episode_start, title.episode_end
        if (
            (start is None) != (end is None)
            or start is not None and end is not None
            and (start <= 0 or end <= 0 or start > end)
        ):
            blockers.append((title, "malformed_manual_split_range"))
        if title.episode_filename_pattern:
            try:
                compile_manual_split_pattern(title.episode_filename_pattern)
            except ValueError:
                blockers.append((title, "invalid_manual_split_pattern"))
    return tuple(blockers)


def _build_assignment_intents(
    collections: list[CatalogCollection],
    titles: list[CatalogTitle],
    videos: list[Video],
    hierarchy: dict[str, HierarchyIdentity],
) -> tuple[
    dict[int, _AssignmentIntent],
    dict[int, ManualSplitVideoDecision],
    list[RebuildBlocker],
]:
    collections_by_path = {item.relative_root_path: item for item in collections}
    titles_by_path = {item.relative_root_path: item for item in titles}
    preliminary_paths: dict[int, str | None] = {}
    manual_candidates: dict[int, tuple[CatalogTitle | None, ReconciliationReason | None]] = {}
    grouped: dict[str, list[Video]] = {}
    blockers: list[RebuildBlocker] = []
    blocked_collection_paths: set[str] = set()
    blocked_video_ids: set[int] = set()
    legacy_ambiguous_video_ids: set[int] = set()
    video_ids = {video.id for video in videos if video.id is not None}
    authority_targets: dict[int, list[CatalogTitle]] = {}
    for title in titles:
        title_collection_path = (
            title.collection.relative_root_path if title.collection is not None else None
        )
        for link in title.manual_split_rule_videos:
            if link.video_id not in video_ids:
                blockers.append(RebuildBlocker(
                    code="dangling_manual_split_authority",
                    collection_path=title_collection_path,
                    title_path=title.relative_root_path,
                    prevents_apply=True,
                ))
                if title_collection_path is not None:
                    blocked_collection_paths.add(title_collection_path)
                continue
            authority_targets.setdefault(link.video_id, []).append(title)

    for video in videos:
        assert video.id is not None
        explicit_targets = authority_targets.get(video.id, [])
        explicit_collection_paths = {
            target.collection.relative_root_path
            for target in explicit_targets
            if target.collection is not None
        }
        if any(target.collection is None for target in explicit_targets):
            blocked_video_ids.add(video.id)
            blockers.append(RebuildBlocker(
                code="orphan_manual_split_authority",
                title_path=next(
                    target.relative_root_path
                    for target in explicit_targets
                    if target.collection is None
                ),
                video_path=video.relative_path,
                prevents_apply=True,
            ))
        if len(explicit_collection_paths) > 1:
            blocked_video_ids.add(video.id)
            blocked_collection_paths.update(explicit_collection_paths)
            blockers.append(RebuildBlocker(
                code="cross_collection_manual_split_authority",
                video_path=video.relative_path,
                prevents_apply=True,
            ))
        if is_root_video(video):
            manual_candidates[video.id] = (None, None)
            collection = meaningful_root_collection(video)
            path = (
                next(iter(explicit_collection_paths))
                if len(explicit_collection_paths) == 1
                else collection.relative_root_path if collection is not None else None
            )
            preliminary_paths[video.id] = path
            if path is not None:
                grouped.setdefault(path, []).append(video)
            continue
        identity = hierarchy[video.relative_path]
        candidate, reason = _manual_assignment_candidate(video, identity, titles_by_path)
        manual_candidates[video.id] = (candidate, reason)
        legacy_conflict_collection = (
            video.catalog_collection
            if video.catalog_title is None
            and video.catalog_collection is not None
            and video.catalog_collection.hierarchy_status == "conflict"
            and manual_split_titles(video.catalog_collection)
            else None
        )
        path = (
            next(iter(explicit_collection_paths))
            if len(explicit_collection_paths) == 1
            else candidate.collection.relative_root_path
            if candidate is not None and candidate.collection is not None
            else legacy_conflict_collection.relative_root_path
            if legacy_conflict_collection is not None
            else identity.collection.relative_root_path
        )
        preliminary_paths[video.id] = path
        grouped.setdefault(path, []).append(video)

    decisions: dict[int, ManualSplitVideoDecision] = {}
    for path, grouped_videos in sorted(grouped.items()):
        if path in blocked_collection_paths:
            continue
        collection = collections_by_path.get(path)
        if collection is None or not manual_split_titles(collection):
            continue
        invalid_titles = _manual_split_title_blockers(collection)
        if invalid_titles:
            blocked_collection_paths.add(path)
            blockers.extend(
                RebuildBlocker(
                    code=code,
                    collection_path=path,
                    title_path=title.relative_root_path,
                    prevents_apply=True,
                )
                for title, code in invalid_titles
            )
            continue
        result = evaluate_persisted_manual_split(
            collection,
            grouped_videos,
            use_fresh_numbering=True,
        )
        for decision in historical_manual_split_ambiguities(collection, result):
            video = decision.video
            assert video.id is not None
            blocked_collection_paths.add(path)
            legacy_ambiguous_video_ids.add(video.id)
            blockers.append(RebuildBlocker(
                code="historical_pre4a_manual_split_conflict",
                collection_path=path,
                video_path=video.relative_path,
                prevents_apply=True,
            ))
        decisions.update(
            (decision.video.id, decision)
            for decision in result.decisions
            if decision.video.id is not None
            and decision.video.id not in legacy_ambiguous_video_ids
        )

    intents: dict[int, _AssignmentIntent] = {}
    for video in videos:
        assert video.id is not None
        preliminary_path = preliminary_paths[video.id]
        candidate, candidate_reason = manual_candidates[video.id]
        if video.id in blocked_video_ids or preliminary_path in blocked_collection_paths:
            current_title = video.catalog_title
            current_collection = (
                current_title.collection if current_title is not None else video.catalog_collection
            )
            intents[video.id] = _AssignmentIntent(
                current_collection.relative_root_path if current_collection else preliminary_path,
                current_title.relative_root_path if current_title else None,
                None,
                ReconciliationReason.CURRENT,
            )
            continue

        decision = decisions.get(video.id)
        if decision is not None and decision.kind == ManualSplitDecisionKind.UNIQUE:
            target = decision.target_catalog_title
            assert target is not None and target.collection is not None
            intents[video.id] = _AssignmentIntent(
                target.collection.relative_root_path,
                target.relative_root_path,
                decision,
                ReconciliationReason.MANUAL_SPLIT_UNIQUE,
            )
        elif decision is not None and decision.kind == ManualSplitDecisionKind.CONFLICT:
            intents[video.id] = _AssignmentIntent(
                preliminary_path,
                None,
                decision,
                ReconciliationReason.MANUAL_SPLIT_CONFLICT,
            )
        elif decision is not None and decision.kind == ManualSplitDecisionKind.UNMATCHED:
            intents[video.id] = _AssignmentIntent(
                preliminary_path,
                None,
                decision,
                ReconciliationReason.MANUAL_SPLIT_UNMATCHED,
            )
        elif decision is not None and decision.kind == ManualSplitDecisionKind.NOT_REQUIRED:
            # The shared evaluator uses NOT_REQUIRED as a compatibility boundary:
            # it never becomes an explicit match or new M:N authority.  A real
            # manual candidate remains authoritative; ordinary stale automatic
            # membership is rebuilt from the physical identity.  Supplementary
            # and secondary duplicate compatibility remains non-destructive.
            if candidate is not None:
                assert candidate.collection is not None
                intents[video.id] = _AssignmentIntent(
                    candidate.collection.relative_root_path,
                    candidate.relative_root_path,
                    decision,
                    candidate_reason or ReconciliationReason.CURRENT,
                )
            elif is_root_video(video):
                collection = meaningful_root_collection(video)
                title = video.catalog_title
                intents[video.id] = _AssignmentIntent(
                    collection.relative_root_path if collection is not None else None,
                    (
                        title.relative_root_path
                        if title is not None and title.collection is collection else None
                    ),
                    decision,
                    (
                        ReconciliationReason.ROOT_PRESERVED
                        if collection is not None else ReconciliationReason.ROOT_UNASSIGNED
                    ),
                )
            elif (
                is_nonprimary_duplicate_video(video)
                or effective_video_numbering(
                    video,
                    use_current_title=False,
                ).is_supplementary
            ):
                current = video.catalog_title
                intents[video.id] = _AssignmentIntent(
                    preliminary_path,
                    (
                        current.relative_root_path
                        if current is not None
                        and current.collection is not None
                        and current.collection.relative_root_path == preliminary_path
                        else None
                    ),
                    decision,
                    ReconciliationReason.CURRENT,
                )
            else:
                identity = hierarchy[video.relative_path]
                intents[video.id] = _AssignmentIntent(
                    identity.collection.relative_root_path,
                    identity.title.relative_root_path,
                    decision,
                    ReconciliationReason.AUTOMATIC_PATH,
                )
        elif is_root_video(video):
            collection = meaningful_root_collection(video)
            if collection is None:
                intents[video.id] = _AssignmentIntent(
                    None, None, None, ReconciliationReason.ROOT_UNASSIGNED,
                )
            else:
                title = video.catalog_title
                title_path = (
                    title.relative_root_path
                    if title is not None and title.collection is collection else None
                )
                intents[video.id] = _AssignmentIntent(
                    collection.relative_root_path,
                    title_path,
                    None,
                    ReconciliationReason.ROOT_PRESERVED,
                )
        elif candidate is not None:
            assert candidate.collection is not None and candidate_reason is not None
            intents[video.id] = _AssignmentIntent(
                candidate.collection.relative_root_path,
                candidate.relative_root_path,
                decision,
                candidate_reason,
            )
        else:
            identity = hierarchy[video.relative_path]
            intents[video.id] = _AssignmentIntent(
                identity.collection.relative_root_path,
                identity.title.relative_root_path,
                decision,
                ReconciliationReason.AUTOMATIC_PATH,
            )
    return intents, decisions, blockers


def _build_title_specs(
    titles: list[CatalogTitle],
    hierarchy: dict[str, HierarchyIdentity],
    intents: dict[int, _AssignmentIntent],
) -> dict[str, _TitleSpec]:
    titles_by_path = {title.relative_root_path: title for title in titles}
    identities_by_title_path = {
        identity.title.relative_root_path: identity for identity in hierarchy.values()
    }
    required_paths = {
        intent.title_path for intent in intents.values() if intent.title_path is not None
    }
    specs: dict[str, _TitleSpec] = {}
    for title in titles:
        protection = _title_protection_reasons(title)
        required = title.relative_root_path in required_paths
        if not protection and not required:
            continue
        identity = identities_by_title_path.get(title.relative_root_path)
        automatic_identity = (
            identity.title
            if identity is not None
            and not manual_hierarchy_snapshot_requires_preservation(title)
            else None
        )
        collection_path = (
            identity.collection.relative_root_path
            if automatic_identity is not None
            else title.collection.relative_root_path
            if title.collection is not None
            else identity.collection.relative_root_path
            if identity is not None
            else None
        )
        specs[title.relative_root_path] = _TitleSpec(
            original=title,
            identity=automatic_identity,
            collection_path=collection_path,
            protected=bool(protection),
            protection_reasons=protection,
        )

    for path in sorted(required_paths):
        if path in specs:
            continue
        identity = identities_by_title_path.get(path)
        if identity is None:
            original = titles_by_path.get(path)
            if original is None or original.collection is None:
                raise HierarchyRebuildError(
                    f"Plán odkazuje na title bez reprodukovatelné collection: {path}"
                )
            specs[path] = _TitleSpec(
                original=original,
                identity=None,
                collection_path=original.collection.relative_root_path,
                protected=True,
                protection_reasons=_title_protection_reasons(original),
            )
            continue
        specs[path] = _TitleSpec(
            original=None,
            identity=identity.title,
            collection_path=identity.collection.relative_root_path,
            protected=False,
            protection_reasons=(),
        )
    return specs


def _build_collection_identities(
    collections: list[CatalogCollection],
    hierarchy: dict[str, HierarchyIdentity],
    title_specs: dict[str, _TitleSpec],
    intents: dict[int, _AssignmentIntent],
) -> dict[str, CollectionIdentity | None]:
    existing_by_path = {item.relative_root_path: item for item in collections}
    parser_identities = {
        identity.collection.relative_root_path: identity.collection
        for identity in hierarchy.values()
    }
    required_paths = {
        intent.collection_path
        for intent in intents.values()
        if intent.collection_path is not None
    }
    required_paths.update(
        spec.collection_path
        for spec in title_specs.values()
        if spec.collection_path is not None
    )
    required_paths.update(
        collection.relative_root_path
        for collection in collections
        if _collection_has_user_state(collection)
    )
    return {
        path: parser_identities.get(path)
        if path in parser_identities else None
        for path in sorted(required_paths)
        if path in existing_by_path or path in parser_identities
    }


def _clone_collection(
    original: CatalogCollection | None,
    identity: CollectionIdentity | None,
    synthetic_id: int,
) -> CatalogCollection:
    local_title = identity.local_title if identity is not None else original.local_title
    path = identity.relative_root_path if identity is not None else original.relative_root_path
    clone = CatalogCollection(
        id=original.id if original is not None else synthetic_id,
        local_title=local_title,
        normalized_local_title=(
            normalize_title(local_title)
            if identity is not None else original.normalized_local_title
        ),
        relative_root_path=path,
        manual_display_title=original.manual_display_title if original else None,
        hierarchy_status=original.hierarchy_status if original else "automatic",
        hierarchy_verified_at=original.hierarchy_verified_at if original else None,
        hierarchy_note=original.hierarchy_note if original else None,
        local_period_hint=(
            extract_local_period_hint(local_title)
            if identity is not None else original.local_period_hint
        ),
    )
    return clone


def _clone_title(spec: _TitleSpec, synthetic_id: int) -> CatalogTitle:
    original = spec.original
    identity = spec.identity
    local_title = identity.local_title if identity is not None else original.local_title
    clone = CatalogTitle(
        id=original.id if original is not None else synthetic_id,
        local_title=local_title,
        normalized_local_title=(
            normalize_title(local_title)
            if identity is not None else original.normalized_local_title
        ),
        relative_root_path=(
            identity.relative_root_path if identity is not None else original.relative_root_path
        ),
        part_type=identity.part_type if identity is not None else original.part_type,
        season_number=identity.season_number if identity is not None else original.season_number,
        part_number=identity.part_number if identity is not None else original.part_number,
        season_label=identity.season_label if identity is not None else original.season_label,
        original_folder_name=(
            identity.original_folder_name if identity is not None
            else original.original_folder_name
        ),
        sort_order=identity.sort_order if identity is not None else original.sort_order,
        episode_start_offset=original.episode_start_offset if original else None,
        numbering_mode=original.numbering_mode if original else "unknown",
        numbering_manual=original.numbering_manual if original else False,
        numbering_verified_at=original.numbering_verified_at if original else None,
        hierarchy_manual_override=original.hierarchy_manual_override if original else False,
        season_number_manual=original.season_number_manual if original else None,
        part_number_manual=original.part_number_manual if original else None,
        season_label_manual=original.season_label_manual if original else None,
        part_type_manual=original.part_type_manual if original else None,
        sort_order_manual=original.sort_order_manual if original else None,
        hierarchy_verified_at=original.hierarchy_verified_at if original else None,
        episode_start=original.episode_start if original else None,
        episode_end=original.episode_end if original else None,
        episode_filename_pattern=original.episode_filename_pattern if original else None,
        manual_display_title=original.manual_display_title if original else None,
        preferred_metadata_provider=original.preferred_metadata_provider if original else None,
        preferred_external_id=original.preferred_external_id if original else None,
        metadata_status=original.metadata_status if original else "unlinked",
        metadata_locked=original.metadata_locked if original else False,
    )
    if original is not None and original.metadata_record is not None:
        clone.metadata_record = TitleMetadata(
            catalog_title_id=clone.id,
            display_title=original.metadata_record.display_title,
            episode_count=original.metadata_record.episode_count,
        )
    if original is not None:
        clone.manual_split_rule_videos = [
            ManualSplitRuleVideo(video_id=link.video_id)
            for link in original.manual_split_rule_videos
        ]
    return clone


def _clone_video(video: Video) -> Video:
    return Video(
        id=video.id,
        relative_path=video.relative_path,
        root_folder=video.root_folder,
        filename=video.filename,
        size=video.size,
        mtime_ns=video.mtime_ns,
        duration=video.duration,
        video_codec=video.video_codec,
        width=video.width,
        height=video.height,
        file_type=video.file_type,
        manual_hardsub_cs=video.manual_hardsub_cs,
        manual_hardsub_sk=video.manual_hardsub_sk,
        manual_hardsub_verified_at=video.manual_hardsub_verified_at,
        czsk_availability_manual=video.czsk_availability_manual,
        local_episode_number=video.local_episode_number,
        season_episode_number=video.season_episode_number,
        absolute_episode_number=video.absolute_episode_number,
        external_episode_number=video.external_episode_number,
        episode_number_source=video.episode_number_source,
        episode_number_confidence=video.episode_number_confidence,
        episode_number_manual_override=video.episode_number_manual_override,
        episode_number_verified_at=video.episode_number_verified_at,
        content_type_manual=video.content_type_manual,
        media_part_number=video.media_part_number,
        duplicate_status_manual=video.duplicate_status_manual,
        duplicate_of_video_id=video.duplicate_of_video_id,
        duplicate_primary_missing=video.duplicate_primary_missing,
    )


def _clear_derived_numbering(video: Video) -> None:
    """Remove rebuildable cache so structural inference sees fresh-scan input."""
    video.local_episode_number = None
    video.season_episode_number = None
    video.absolute_episode_number = None
    video.external_episode_number = None
    video.episode_number_source = "unknown"
    video.episode_number_confidence = None


def _build_projection(
    collections: list[CatalogCollection],
    videos: list[Video],
    collection_identities: dict[str, CollectionIdentity | None],
    title_specs: dict[str, _TitleSpec],
    intents: dict[int, _AssignmentIntent],
    blocked_paths: set[str],
) -> _Projection:
    existing_collections = {item.relative_root_path: item for item in collections}
    projected_collections: dict[str, CatalogCollection] = {}
    next_collection_id = -1
    for path, identity in collection_identities.items():
        projected_collections[path] = _clone_collection(
            existing_collections.get(path), identity, next_collection_id,
        )
        if existing_collections.get(path) is None:
            next_collection_id -= 1

    projected_titles: dict[str, CatalogTitle] = {}
    next_title_id = -1
    for path, spec in sorted(title_specs.items()):
        clone = _clone_title(spec, next_title_id)
        if spec.original is None:
            next_title_id -= 1
        clone.collection = (
            projected_collections[spec.collection_path]
            if spec.collection_path is not None else None
        )
        clone.catalog_collection_id = clone.collection.id if clone.collection else None
        projected_titles[path] = clone

    projected_videos: dict[int, Video] = {}
    for video in videos:
        assert video.id is not None
        clone = _clone_video(video)
        intent = intents[video.id]
        if intent.collection_path is not None:
            clone.catalog_collection = projected_collections[intent.collection_path]
            clone.catalog_collection_id = clone.catalog_collection.id
        else:
            clone.catalog_collection = None
            clone.catalog_collection_id = None
        if intent.title_path is not None:
            clone.catalog_title = projected_titles[intent.title_path]
            clone.catalog_title_id = clone.catalog_title.id
            if clone.catalog_title.collection is not clone.catalog_collection:
                raise HierarchyRebuildError(
                    f"Plán vytvořil nekonzistentní assignment pro {video.relative_path}."
                )
        else:
            clone.catalog_title = None
            clone.catalog_title_id = None
        projected_videos[video.id] = clone

    for original in videos:
        if original.id is None or original.duplicate_of_video_id is None:
            continue
        clone = projected_videos[original.id]
        primary = projected_videos.get(original.duplicate_of_video_id)
        if primary is not None:
            clone.duplicate_of = primary
            clone.duplicate_of_video_id = primary.id

    for video in projected_videos.values():
        collection_path = (
            video.catalog_collection.relative_root_path
            if video.catalog_collection is not None else None
        )
        if collection_path not in blocked_paths:
            _clear_derived_numbering(video)

    evaluations: dict[str, HierarchyEvaluationResult] = {}
    for path, collection in sorted(projected_collections.items()):
        if path in blocked_paths or not collection.videos:
            continue
        evaluations[path] = finalize_collection_hierarchy(
            collection,
            list(collection.videos),
            include_legacy_fallback=False,
        )
    return _Projection(
        projected_collections,
        projected_titles,
        projected_videos,
        evaluations,
    )


def _issue_plan_items(
    evaluations: dict[str, HierarchyEvaluationResult],
) -> tuple[HierarchyIssuePlanItem, ...]:
    items = []
    for collection_path, evaluation in sorted(evaluations.items()):
        for issue in evaluation.issues:
            items.append(HierarchyIssuePlanItem(
                collection_path=collection_path,
                code=issue.code.value,
                blocking=issue.blocking,
                scope=issue.scope.value,
                title_path=(
                    issue.catalog_title.relative_root_path
                    if issue.catalog_title is not None else None
                ),
                video_paths=tuple(sorted(video.relative_path for video in issue.videos)),
                related_title_paths=tuple(sorted(
                    title.relative_root_path for title in issue.related_catalog_titles
                )),
            ))
    return tuple(sorted(
        items,
        key=lambda item: (
            item.collection_path,
            item.code,
            item.title_path or "",
            item.video_paths,
        ),
    ))


def build_hierarchy_rebuild_plan(session: Session) -> HierarchyRebuildPlan:
    """Build a complete reconciliation preview without mutating the Session or DB."""
    _require_clean_unit_of_work(session)
    collections, titles, videos = _load_state(session)
    fingerprint = _state_fingerprint(collections, titles, videos)
    hierarchy = derive_library_hierarchy([video.relative_path for video in videos])
    intents, decisions, blockers = _build_assignment_intents(
        collections, titles, videos, hierarchy,
    )
    title_specs = _build_title_specs(titles, hierarchy, intents)
    collection_identities = _build_collection_identities(
        collections, hierarchy, title_specs, intents,
    )
    blocked_paths = {
        blocker.collection_path
        for blocker in blockers
        if blocker.prevents_apply and blocker.collection_path is not None
    }
    projection = _build_projection(
        collections,
        videos,
        collection_identities,
        title_specs,
        intents,
        blocked_paths,
    )

    original_collections = {item.relative_root_path: item for item in collections}
    collection_items: list[CollectionPlanItem] = []
    for path in sorted(set(original_collections) | set(projection.collections)):
        original = original_collections.get(path)
        desired = projection.collections.get(path)
        current_snapshot = _collection_snapshot(original) if original else None
        desired_snapshot = _collection_snapshot(desired) if desired else None
        if original is None:
            action = ReconciliationAction.CREATE
            reason = ReconciliationReason.AUTOMATIC_PATH
        elif desired is None:
            action = ReconciliationAction.REMOVE
            reason = ReconciliationReason.AUTOMATIC_OBSOLETE
        elif current_snapshot != desired_snapshot:
            action = ReconciliationAction.UPDATE
            reason = ReconciliationReason.AUTOMATIC_PATH
        else:
            action = ReconciliationAction.PRESERVE
            reason = ReconciliationReason.CURRENT
        collection_items.append(CollectionPlanItem(
            action=action,
            collection_id=original.id if original else None,
            relative_root_path=path,
            current=current_snapshot,
            desired=desired_snapshot,
            reason=reason,
        ))

    original_titles = {item.relative_root_path: item for item in titles}
    title_items: list[TitlePlanItem] = []
    target_title_paths = {
        intent.title_path for intent in intents.values() if intent.title_path is not None
    }
    for path in sorted(set(original_titles) | set(projection.titles)):
        original = original_titles.get(path)
        desired = projection.titles.get(path)
        spec = title_specs.get(path)
        protection = spec.protection_reasons if spec is not None else ()
        current_snapshot = _title_snapshot(original) if original else None
        desired_snapshot = _title_snapshot(desired) if desired else None
        if original is None:
            action = ReconciliationAction.CREATE
            reason = ReconciliationReason.AUTOMATIC_PATH
        elif desired is None:
            action = ReconciliationAction.REMOVE
            reason = ReconciliationReason.AUTOMATIC_OBSOLETE
        elif current_snapshot != desired_snapshot:
            action = ReconciliationAction.UPDATE
            reason = (
                ReconciliationReason.PROTECTED_USER_DATA
                if protection else ReconciliationReason.AUTOMATIC_PATH
            )
        else:
            action = ReconciliationAction.PRESERVE
            reason = (
                ReconciliationReason.PROTECTED_USER_DATA
                if protection else ReconciliationReason.CURRENT
            )
        title_items.append(TitlePlanItem(
            action=action,
            title_id=original.id if original else None,
            relative_root_path=path,
            current=current_snapshot,
            desired=desired_snapshot,
            protected=bool(protection),
            protection_reasons=protection,
            reason=reason,
        ))
        if (
            protection
            and path not in target_title_paths
            and original is not None
            and not original.hierarchy_manual_override
        ):
            blockers.append(RebuildBlocker(
                code="protected_obsolete_title",
                collection_path=desired_snapshot.collection_path if desired_snapshot else None,
                title_path=path,
                prevents_apply=False,
            ))

    assignment_items = []
    numbering_items = []
    for video in videos:
        assert video.id is not None
        intent = intents[video.id]
        decision = decisions.get(video.id)
        matching_titles = (
            decision.matching_catalog_titles if decision is not None else ()
        )
        assignment_items.append(VideoAssignmentPlanItem(
            video_id=video.id,
            relative_path=video.relative_path,
            old_collection_path=_current_collection_path(video),
            old_title_path=_current_title_path(video),
            target_collection_path=intent.collection_path,
            target_title_path=intent.title_path,
            manual_split_kind=decision.kind.value if decision is not None else None,
            matched_manual_title_ids=tuple(
                title.id for title in matching_titles if title.id is not None
            ),
            matched_manual_title_paths=tuple(
                title.relative_root_path for title in matching_titles
            ),
            reason=intent.reason,
        ))
        desired_numbering = _numbering_snapshot(projection.videos[video.id])
        current_numbering = _numbering_snapshot(video)
        if current_numbering != desired_numbering:
            numbering_items.append(VideoNumberingPlanItem(
                video_id=video.id,
                relative_path=video.relative_path,
                current=current_numbering,
                desired=desired_numbering,
            ))

    return HierarchyRebuildPlan(
        source_fingerprint=fingerprint,
        collections=tuple(collection_items),
        titles=tuple(title_items),
        video_assignments=tuple(assignment_items),
        numbering=tuple(numbering_items),
        issues=_issue_plan_items(projection.evaluations),
        blockers=tuple(sorted(
            blockers,
            key=lambda item: (
                item.prevents_apply,
                item.code,
                item.collection_path or "",
                item.title_path or "",
                item.video_path or "",
            ),
        )),
    )


def _apply_collection_snapshot(
    collection: CatalogCollection,
    snapshot: CollectionSnapshot,
) -> None:
    collection.local_title = snapshot.local_title
    collection.normalized_local_title = snapshot.normalized_local_title
    collection.local_period_hint = snapshot.local_period_hint


def _apply_title_snapshot(
    title: CatalogTitle,
    snapshot: TitleSnapshot,
    collections_by_path: dict[str, CatalogCollection],
) -> None:
    title.local_title = snapshot.local_title
    title.normalized_local_title = snapshot.normalized_local_title
    title.relative_root_path = snapshot.relative_root_path
    title.part_type = snapshot.part_type
    title.season_number = snapshot.season_number
    title.part_number = snapshot.part_number
    title.season_label = snapshot.season_label
    title.original_folder_name = snapshot.original_folder_name
    title.sort_order = snapshot.sort_order
    title.collection = (
        collections_by_path[snapshot.collection_path]
        if snapshot.collection_path is not None else None
    )


def _reload_collections(session: Session) -> dict[str, CatalogCollection]:
    session.expire_all()
    return {
        collection.relative_root_path: collection
        for collection in session.scalars(
            select(CatalogCollection).options(
                selectinload(CatalogCollection.titles).selectinload(CatalogTitle.videos),
                selectinload(CatalogCollection.titles).selectinload(
                    CatalogTitle.metadata_record
                ),
                selectinload(CatalogCollection.titles).selectinload(
                    CatalogTitle.manual_split_rule_videos
                ),
                selectinload(CatalogCollection.videos).selectinload(Video.catalog_title),
            )
        )
    }


def _verify_applied_plan(session: Session, plan: HierarchyRebuildPlan) -> None:
    collections, titles, videos = _load_state(session)
    collections_by_path = {item.relative_root_path: item for item in collections}
    titles_by_path = {item.relative_root_path: item for item in titles}
    videos_by_id = {item.id: item for item in videos}

    for item in plan.collections:
        if item.desired is None:
            if item.relative_root_path in collections_by_path:
                raise HierarchyRebuildError(
                    f"Obsolete collection nebyla odstraněna: {item.relative_root_path}"
                )
            continue
        actual = collections_by_path.get(item.relative_root_path)
        if actual is None or _collection_snapshot(actual) != item.desired:
            raise HierarchyRebuildError(
                f"Apply se odchýlil od preview collection: {item.relative_root_path}"
            )
    for item in plan.titles:
        if item.desired is None:
            if item.relative_root_path in titles_by_path:
                raise HierarchyRebuildError(
                    f"Obsolete title nebyl odstraněn: {item.relative_root_path}"
                )
            continue
        actual = titles_by_path.get(item.relative_root_path)
        if actual is None or _title_snapshot(actual) != item.desired:
            raise HierarchyRebuildError(
                f"Apply se odchýlil od preview title: {item.relative_root_path}"
            )
    for item in plan.video_assignments:
        video = videos_by_id[item.video_id]
        if (
            _current_collection_path(video),
            _current_title_path(video),
        ) != (
            item.target_collection_path,
            item.target_title_path,
        ):
            raise HierarchyRebuildError(
                f"Apply se odchýlil od preview assignmentu: {item.relative_path}"
            )
        if (
            video.catalog_title is not None
            and video.catalog_title.collection is not video.catalog_collection
        ):
            raise HierarchyRebuildError(
                f"Nekonzistentní redundantní collection FK: {item.relative_path}"
            )
    desired_numbering = {item.video_id: item.desired for item in plan.numbering}
    for video_id, desired in desired_numbering.items():
        if _numbering_snapshot(videos_by_id[video_id]) != desired:
            raise HierarchyRebuildError(
                f"Apply se odchýlil od preview numberingu: {videos_by_id[video_id].relative_path}"
            )


def apply_hierarchy_rebuild_plan(
    session: Session,
    plan: HierarchyRebuildPlan,
) -> HierarchyRebuildResult:
    """Apply one previously built plan; transaction commit remains with the caller."""
    _require_clean_unit_of_work(session)
    # A plan may be held while another transaction changes authority/user data.
    # Force the identity map to observe the current committed rows before the
    # optimistic fingerprint check.
    session.expire_all()
    collections, titles, videos = _load_state(session)
    if _state_fingerprint(collections, titles, videos) != plan.source_fingerprint:
        raise HierarchyPlanStaleError(
            "Hierarchy rebuild plan už neodpovídá aktuálnímu stavu databáze."
        )
    hard_blockers = tuple(item for item in plan.blockers if item.prevents_apply)
    if hard_blockers:
        raise HierarchyPlanBlockedError(
            "Hierarchy rebuild nelze bezpečně aplikovat kvůli structured blockeru: "
            + ", ".join(item.code for item in hard_blockers)
        )
    if not plan.has_changes:
        return HierarchyRebuildResult(plan=plan, applied=True)

    with session.begin_nested():
        collections_by_path = {item.relative_root_path: item for item in collections}
        for item in plan.collections:
            if item.action == ReconciliationAction.CREATE:
                assert item.desired is not None
                collection = CatalogCollection(
                    local_title=item.desired.local_title,
                    normalized_local_title=item.desired.normalized_local_title,
                    relative_root_path=item.desired.relative_root_path,
                    local_period_hint=item.desired.local_period_hint,
                )
                session.add(collection)
                collections_by_path[item.relative_root_path] = collection
            elif item.desired is not None:
                _apply_collection_snapshot(
                    collections_by_path[item.relative_root_path], item.desired,
                )
        session.flush()

        titles_by_path = {item.relative_root_path: item for item in titles}
        for item in plan.titles:
            if item.action == ReconciliationAction.CREATE:
                assert item.desired is not None
                title = CatalogTitle(
                    local_title=item.desired.local_title,
                    normalized_local_title=item.desired.normalized_local_title,
                    relative_root_path=item.desired.relative_root_path,
                )
                _apply_title_snapshot(title, item.desired, collections_by_path)
                session.add(title)
                titles_by_path[item.relative_root_path] = title
            elif item.desired is not None:
                _apply_title_snapshot(
                    titles_by_path[item.relative_root_path],
                    item.desired,
                    collections_by_path,
                )
        session.flush()

        videos_by_id = {video.id: video for video in videos}
        for item in plan.video_assignments:
            video = videos_by_id[item.video_id]
            video.catalog_collection = (
                collections_by_path[item.target_collection_path]
                if item.target_collection_path is not None else None
            )
            video.catalog_title = (
                titles_by_path[item.target_title_path]
                if item.target_title_path is not None else None
            )
            if (
                video.catalog_title is not None
                and video.catalog_title.collection is not video.catalog_collection
            ):
                raise HierarchyRebuildError(
                    f"Nelze aplikovat nekonzistentní assignment: {item.relative_path}"
                )
        session.flush()

        for item in plan.titles:
            if item.action != ReconciliationAction.REMOVE:
                continue
            title = titles_by_path[item.relative_root_path]
            if title.videos or title.manual_split_rule_videos:
                raise HierarchyRebuildError(
                    f"Obsolete title už není prázdný: {item.relative_root_path}"
                )
            if title.collection is not None and title in title.collection.titles:
                title.collection.titles.remove(title)
            title.collection = None
            session.delete(title)
            titles_by_path.pop(item.relative_root_path, None)
        session.flush()

        for item in plan.collections:
            if item.action != ReconciliationAction.REMOVE:
                continue
            collection = collections_by_path[item.relative_root_path]
            if collection.titles or collection.videos:
                raise HierarchyRebuildError(
                    f"Obsolete collection už není prázdná: {item.relative_root_path}"
                )
            session.delete(collection)
            collections_by_path.pop(item.relative_root_path, None)
        session.flush()

        for video in videos_by_id.values():
            _clear_derived_numbering(video)
        session.flush()

        collections_by_path = _reload_collections(session)
        for path, collection in sorted(collections_by_path.items()):
            if not collection.videos:
                continue
            finalize_collection_hierarchy(
                collection,
                list(collection.videos),
                include_legacy_fallback=False,
            )
        session.flush()
        _verify_applied_plan(session, plan)
        return HierarchyRebuildResult(plan=plan, applied=True)


def rebuild_hierarchy(
    session: Session,
    *,
    apply: bool = False,
) -> HierarchyRebuildResult:
    """Compatibility orchestration used by the CLI and existing Python callers."""
    plan = build_hierarchy_rebuild_plan(session)
    if not apply:
        return HierarchyRebuildResult(plan=plan, applied=False)
    result = apply_hierarchy_rebuild_plan(session, plan)
    session.commit()
    return result
